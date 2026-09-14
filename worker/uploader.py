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
    client = TelegramClient(
        StringSession(cfg.telethon_session),
        cfg.api_id,
        cfg.api_hash,
        flood_sleep_threshold=120,
    )
    try:
        await asyncio.wait_for(client.start(), timeout=180)
    except Exception as e:
        await _safe_disconnect(client)
        raise UploadError(f"không kết nối được Telegram: {e}") from None

    def progress(sent: int, total: int) -> None:
        if total and sent * 100 // total % 10 == 0:
            log.info("upload %s: %d%%", caption, sent * 100 // total)

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
        raise UploadError(f"send_file thất bại: {e}") from None
    finally:
        await _safe_disconnect(client)
