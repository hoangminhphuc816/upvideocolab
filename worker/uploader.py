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

    try:
        msg = await client.send_file(
            cfg.target_channel,
            path,
            caption=caption[:1024],
            supports_streaming=True,
            progress_callback=progress,
        )
        return msg.id
    except Exception as e:
        raise UploadError(f"send_file thất bại: {type(e).__name__}: {e}") from None
    finally:
        await _safe_disconnect(client)
