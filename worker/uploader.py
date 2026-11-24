"""Upload MP4 vào kênh Telegram riêng tư qua Telethon MTProto."""
from __future__ import annotations

import asyncio
import logging
import os

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, ServerError, TimedOutError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

from worker.config import Config
from worker.downloader import extract_thumbnail, probe_video_metadata

log = logging.getLogger(__name__)


class UploadError(Exception):
    pass


class TransientUploadError(UploadError):
    """Lỗi tạm thời ở pha resolve/upload — sweep RETRY cứu được.
    ``_permanent()`` trong worker/main.py trả False cho lớp này, nên job về
    RETRY thay vì FAILED.
    """


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        log.exception("disconnect lỗi (bỏ qua)")


# Lỗi tạm thời phía hạ tầng/mạng. `asyncio.TimeoutError` là alias của `TimeoutError`
# (cả hai ⊂ `OSError`) nên các tên đó dư thừa về mặt isinstance, nhưng giữ lại để
# đọc rõ ý định. `ServerError`/`TimedOutError` là RPCError, KHÔNG phải OSError —
# thiếu chúng thì get_dialogs fail bằng lỗi đó sẽ rơi xuống get_entity, gặp
# ChannelInvalidError và thành FAILED vĩnh viễn (đúng chuỗi sự cố cũ).
_TRANSIENT_EXC = (
    FloodWaitError, ServerError, TimedOutError,
    asyncio.TimeoutError, TimeoutError, ConnectionError, OSError,
)


def _raise_target_error(target: int, exc: Exception) -> None:
    """Ném đúng lớp lỗi: tạm thời ⇒ TransientUploadError, cấu hình ⇒ UploadError."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, _TRANSIENT_EXC):
        raise TransientUploadError(
            f"TARGET_CHANNEL tạm thời không truy cập được ({target}): {detail}"
        ) from None
    raise UploadError(
        f"TARGET_CHANNEL không truy cập được ({target}): {detail}"
    ) from None


async def resolve_target_channel(cfg: Config):
    """Resolve TARGET_CHANNEL thành InputPeer có access_hash — gọi TRƯỚC khi tải.

    `StringSession` là MemorySession: `_entities` không được serialize (chỉ
    dc_id/ip/port/auth_key), nên mỗi VM mới bắt đầu với cache rỗng. `get_dialogs`
    là cách duy nhất nạp access_hash. Nếu nó fail (FloodWait, timeout), Telethon
    rơi xuống `GetChannelsRequest(InputChannel(id, access_hash=0))` ⇒
    ChannelInvalidError ⇒ ValueError khó hiểu. Gọi sớm để lỗi nêu đích danh
    TARGET_CHANNEL và không đốt một lượt tải video.

    Client LUÔN được disconnect trước khi hàm trả về hoặc ném.
    """
    try:
        session = StringSession(cfg.telethon_session)
    except Exception as e:
        raise UploadError(
            f"TELETHON_SESSION không hợp lệ: {type(e).__name__}: {e}"
        ) from None

    try:
        client = TelegramClient(
            session,
            cfg.api_id,
            cfg.api_hash,
            device_model="Desktop",
            system_version="Windows 11 x64",
            app_version="7.2.8",
            lang_code="en",
            system_lang_code="en-US",
            flood_sleep_threshold=120,
        )
    except Exception as e:
        raise UploadError(f"không tạo được TelegramClient: {type(e).__name__}: {e}") from None

    try:
        await asyncio.wait_for(client.start(), timeout=180)
    except Exception as e:
        await _safe_disconnect(client)
        raise UploadError(f"không kết nối được Telegram: {type(e).__name__}: {e}") from None

    try:
        try:
            await client.get_dialogs()
        except Exception as e:
            log.exception("warm entity cache lỗi (vẫn thử get_entity trực tiếp)")
            if isinstance(e, _TRANSIENT_EXC):
                _raise_target_error(cfg.target_channel, e)

        try:
            entity = await client.get_entity(cfg.target_channel)
        except Exception as e:
            _raise_target_error(cfg.target_channel, e)

        try:
            return utils.get_input_peer(entity)
        except TypeError as e:
            raise UploadError(
                f"TARGET_CHANNEL không dùng được ({cfg.target_channel}): {e}. "
                f"Chạy tools/check_telegram_target.py — access_hash phải khác 0."
            ) from None
    finally:
        await _safe_disconnect(client)


async def upload_to_channel(cfg: Config, path: str, caption: str, *,
                            channel, heartbeat=None) -> int:
    try:
        session = StringSession(cfg.telethon_session)
    except Exception as e:
        raise UploadError(
            f"TELETHON_SESSION không hợp lệ: {type(e).__name__}: {e}"
        ) from None
    try:
        client = TelegramClient(
            session,
            cfg.api_id,
            cfg.api_hash,
            # Giả lập client Telegram Desktop chính thức — fingerprint ổn định,
            # tránh user-agent Telethon mặc định ("PC 64bit"/kernel-release/1.45.0)
            # dễ bị nhận diện client không chính thống (runbook §3a).
            device_model="Desktop",
            system_version="Windows 11 x64",
            app_version="7.2.8",
            lang_code="en",
            system_lang_code="en-US",
            flood_sleep_threshold=120,
        )
    except Exception as e:
        raise UploadError(f"không tạo được TelegramClient: {type(e).__name__}: {e}") from None

    try:
        await asyncio.wait_for(client.start(), timeout=180)
    except Exception as e:
        await _safe_disconnect(client)
        raise UploadError(f"không kết nối được Telegram: {type(e).__name__}: {e}") from None

    last = -1

    def progress(sent: int, total: int) -> None:
        nonlocal last
        if not total:
            return
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                log.exception("heartbeat upload loi (bo qua): %s", caption)
        pct = sent * 100 // total
        if pct % 10 == 0 and pct != last:
            last = pct
            log.info("upload %s: %d%%", caption, pct)

    # Metadata video (width/height/duration) — Telegram player cần để stream
    # đúng: thiếu → màn đen trên web, sai tỉ lệ khung trên app.
    try:
        width, height, duration = probe_video_metadata(path)
    except Exception:
        width, height, duration = 0, 0, 0
    attributes = ([DocumentAttributeVideo(
        w=width, h=height, duration=duration, supports_streaming=True
    )] if width and height else None)

    thumb_path = None
    try:
        seek = 1.0 if duration <= 4 else min(duration / 4, 30.0)
        thumb_path = extract_thumbnail(
            path, os.path.join(os.path.dirname(path), "thumb.jpg"),
            seek_seconds=seek)
    except Exception:
        log.exception("trích thumbnail lỗi (bỏ qua): %s", caption)
        thumb_path = None

    try:
        msg = await client.send_file(
            channel,
            path,
            caption=caption[:1024],
            supports_streaming=True,
            attributes=attributes,
            thumb=thumb_path,
            progress_callback=progress,
        )
        return msg.id
    except Exception as e:
        raise UploadError(f"send_file thất bại: {type(e).__name__}: {e}") from None
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.unlink(thumb_path)
            except OSError:
                log.exception("không xóa được thumb tạm: %s", thumb_path)
        await _safe_disconnect(client)
