"""Chẩn đoán TARGET_CHANNEL từ máy local — KHÔNG chạy trên CI.

Dùng khi worker báo `TARGET_CHANNEL không truy cập được (...)`. Trả lời dứt điểm:
kênh có tồn tại, account có access_hash cho kênh đó, tài khoản có phải admin.

    set -a; source .env; set +a
    .venv/bin/python tools/check_telegram_target.py

Mã thoát: 0 = kênh truy cập được, 1 = có vấn đề.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient, utils
from telethon.sessions import StringSession

from worker.config import Config, DIAGNOSE_REQUIRED, load_config
from worker.uploader import UploadError


async def diagnose(cfg: Config) -> dict:
    client = TelegramClient(
        StringSession(cfg.telethon_session), cfg.api_id, cfg.api_hash,
        device_model="Desktop",
        system_version="Windows 11 x64",
        app_version="7.2.8",
        lang_code="en",
        system_lang_code="en-US",
        flood_sleep_threshold=120,
    )
    try:
        await asyncio.wait_for(client.start(), timeout=180)
        dialogs = await client.get_dialogs()
        try:
            entity = await client.get_entity(cfg.target_channel)
        except Exception as e:
            raise UploadError(
                f"TARGET_CHANNEL không truy cập được ({cfg.target_channel}): "
                f"{type(e).__name__}: {e}"
            ) from None

        is_admin = None
        try:
            # PHẢI truyền user: `get_permissions(entity)` không có user= trả
            # `default_banned_rights` của kênh (ChatBannedRights), KHÔNG phải
            # quyền của account — is_admin sẽ luôn False một cách vô nghĩa.
            # "me" được get_input_entity map thẳng sang InputPeerSelf.
            perms = await client.get_permissions(entity, "me")
            is_admin = bool(getattr(perms, "is_admin", False)
                            or getattr(perms, "is_creator", False))
        except Exception:
            is_admin = None  # không kết luận được

        # access_hash 0/None = account chưa "gặp" kênh trong đời sống session này.
        # InputPeerChat (nhóm cũ -123…) KHÔNG có access_hash — bình thường.
        # Chỉ InputPeerChannel mới cần hash.
        from telethon.tl import types as tl_types
        try:
            peer = utils.get_input_peer(entity)
        except TypeError:
            peer = None
        access_hash = getattr(peer, 'access_hash', 0) if peer else 0
        needs_hash = isinstance(peer, tl_types.InputPeerChannel) or (
            peer is None and isinstance(entity, tl_types.Channel)
        )

        return {
            "entity_type": type(entity).__name__,
            "entity_id": getattr(entity, "id", 0),
            "title": getattr(entity, "title", ""),
            "access_hash": access_hash,
            "is_admin": is_admin,
            "dialogs": len(dialogs),
            "needs_hash": needs_hash,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def main() -> int:
    # Chỉ cần 4 biến đăng nhập Telegram — không cần Sheet/GCP/bot như worker.
    cfg = load_config(required=DIAGNOSE_REQUIRED)
    try:
        info = asyncio.run(diagnose(cfg))
    except UploadError as e:
        print(f"::error::{e}", file=sys.stderr)
        print(
            "\nCách sửa:\n"
            "  1. Forward 1 tin từ kênh sang @userinfobot -> lấy đúng -100xxxxxxxxxx.\n"
            "  2. Thêm account (chủ session) làm admin kênh.\n"
            "  3. Nếu access_hash == 0 dù đã admin: mở kênh đó trong app Telegram\n"
            "     một lần (buộc account 'gặp' kênh để Telegram cấp access_hash).",
            file=sys.stderr,
        )
        return 1

    print(f"loại entity      : {info['entity_type']}")
    print(f"entity id        : {info['entity_id']}")
    print(f"title            : {info['title']}")
    print(f"access_hash      : {info['access_hash']}")
    admin_txt = "không kết luận được" if info["is_admin"] is None else (
        "có" if info["is_admin"] else "KHÔNG")
    print(f"admin            : {admin_txt}")
    print(f"số dialog nạp được: {info['dialogs']}")

    problems = []
    if info["needs_hash"] and info["access_hash"] == 0:
        problems.append("access_hash == 0 — account chưa 'gặp' kênh này")
    if info["is_admin"] is False:
        problems.append("account KHÔNG phải admin kênh — cần quyền gửi tin")
    if info["dialogs"] == 0:
        problems.append("get_dialogs trả 0 — kênh không nằm trong danh sách hội thoại")
    if problems:
        for p in problems:
            print(f"::warning::{p}", file=sys.stderr)
        return 1
    print("OK: kênh truy cập được, có access_hash, account là admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
