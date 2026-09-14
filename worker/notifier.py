"""Thông báo kết quả job tới chủ sở hữu qua bot Telegram (Bot API sendMessage)."""
from __future__ import annotations

import logging

import requests

from worker.config import Config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


class NotifyError(Exception):
    pass


def _send(cfg: Config, text: str) -> None:
    try:
        resp = requests.post(
            API.format(token=cfg.bot_token),
            json={"chat_id": cfg.owner_chat_id, "text": text},
            timeout=30,
        )
    except requests.RequestException as e:
        raise NotifyError(f"không gửi được thông báo: {e}") from None
    if resp.status_code != 200 or not resp.json().get("ok"):
        raise NotifyError(
            f"bot API lỗi: {resp.json().get('description', resp.status_code)}"
        )


def notify_done(cfg: Config, job_id: str, msg_id: int) -> None:
    _send(cfg, f"✅ {job_id} đã upload lên kênh (message_id={msg_id})")


def notify_failed(cfg: Config, job_id: str, reason: str) -> None:
    _send(cfg, f"❌ {job_id} thất bại: {reason}")
