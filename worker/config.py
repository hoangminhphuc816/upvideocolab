"""Load và validate cấu hình worker từ biến môi trường."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable

_REQUIRED = [
    "TELETHON_SESSION",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TARGET_CHANNEL",
    "OWNER_CHAT_ID",
    "GCP_SA_JSON",
    "SHEET_ID",
    "BOT_TOKEN",
]

# Sweep (cron) chỉ cần các biến này — không cần session Telethon
SWEEP_REQUIRED = ["GCP_SA_JSON", "SHEET_ID", "BOT_TOKEN", "OWNER_CHAT_ID"]


@dataclass(frozen=True)
class Config:
    telethon_session: str
    api_id: int
    api_hash: str
    target_channel: int
    owner_chat_id: int
    sa_json: str
    sheet_id: str
    bot_token: str
    sheet_name: str
    dl_dir: str


def load_config(required: Iterable[str] | None = None) -> Config:
    req = list(required) if required is not None else _REQUIRED
    missing = [k for k in req if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Thiếu biến môi trường bắt buộc: {', '.join(missing)}")

    sa_json = os.environ.get("GCP_SA_JSON", "")
    if "GCP_SA_JSON" in req and sa_json:
        try:
            json.loads(sa_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"GCP_SA_JSON không phải JSON hợp lệ: {e}") from None

    def _int(name: str) -> int:
        raw = os.environ.get(name, "")
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} phải là số nguyên, nhận được: {raw!r}") from None

    target_channel = _int("TARGET_CHANNEL")
    # Telethon map id KHÔNG ÂM thành PeerUser (resolve_id: >=0 → PeerUser) ⇒ send_file
    # ném "Could not find the input entity for PeerUser" chỉ SAU khi đã tải xong video
    # trên VM, đốt trọn 1 session Colab. Bắt ở đây để chết ngay trên runner.
    # Chỉ chặn >= 0: nhóm cũ (-123…) là PeerChat và vẫn gửi được.
    # `0` chỉ hợp lệ với sweep (required=SWEEP_REQUIRED, không cần kênh).
    if target_channel > 0 or ("TARGET_CHANNEL" in req and target_channel == 0):
        raise ValueError(
            f"TARGET_CHANNEL phải là id ÂM (kênh: -100xxxxxxxxxx, nhóm cũ: -123…), "
            f"nhận được {target_channel}. Lấy bằng cách forward 1 tin từ kênh sang "
            f"@userinfobot (runbook §1)."
        )

    return Config(
        telethon_session=os.environ.get("TELETHON_SESSION", ""),
        api_id=_int("TELEGRAM_API_ID"),
        api_hash=os.environ.get("TELEGRAM_API_HASH", ""),
        target_channel=target_channel,
        owner_chat_id=_int("OWNER_CHAT_ID"),
        sa_json=sa_json,
        sheet_id=os.environ.get("SHEET_ID", ""),
        bot_token=os.environ.get("BOT_TOKEN", ""),
        sheet_name=os.environ.get("SHEET_NAME", "Jobs"),
        dl_dir=os.environ.get("DL_DIR", "/tmp"),
    )
