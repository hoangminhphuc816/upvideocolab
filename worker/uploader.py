"""Upload MP4 vào kênh Telegram riêng tư qua Telethon MTProto."""
from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from worker.config import Config

log = logging.getLogger(__name__)


class UploadError(Exception):
    pass


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await client.disconnect()
    except Exception:
        log.exception("disconnect lỗi (bỏ qua)")


async def upload_to_channel(cfg: Config, path: str, caption: str) -> int:
    try:
        client = TelegramClient(
            StringSession(cfg.telethon_session),
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

    try:
        await client.get_dialogs()  # warm entity cache (các dialog của account)
    except Exception:
        pass  # cache warm fail không chặn — send_file sẽ tự resolve
    last = -1

    def progress(sent: int, total: int) -> None:
        nonlocal last
        if not total:
            return
        pct = sent * 100 // total
        if pct % 10 == 0 and pct != last:
            last = pct
            log.info("upload %s: %d%%", caption, pct)

    # Metadata video (width/height/duration) — Telegram player cần để stream
    # đúng: thiếu → màn đen trên web, sai tỉ lệ khung trên app.
    from worker.downloader import probe_video_metadata
    from telethon.tl.types import DocumentAttributeVideo
    try:
        width, height, duration = probe_video_metadata(path)
    except Exception:
        width, height, duration = 0, 0, 0
    attributes = ([DocumentAttributeVideo(
        w=width, h=height, duration=duration, supports_streaming=True
    )] if width and height else None)

    try:
        msg = await client.send_file(
            cfg.target_channel,
            path,
            caption=caption[:1024],
            supports_streaming=True,
            attributes=attributes,
            progress_callback=progress,
        )
        return msg.id
    except Exception as e:
        raise UploadError(f"send_file thất bại: {type(e).__name__}: {e}") from None
    finally:
        await _safe_disconnect(client)
