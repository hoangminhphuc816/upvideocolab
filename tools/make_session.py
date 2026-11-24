"""Chạy LOCAL 1 lần để tạo StringSession Telethon (KHÔNG chạy trên CI).

Cách dùng:
    pip install telethon
    python tools/make_session.py
    # Nhập api_id/api_hash (lấy ở https://my.telegram.org → API development tools)
    # Nhập số điện thoại + mã code Telegram gửi về.
    # Copy chuỗi StringSession in ra → dán vào Actions Secret TELETHON_SESSION.

Chuỗi này = full quyền tài khoản Telegram của bạn. KHÔNG commit, không chia sẻ.
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


def la_bot_token(s: str) -> bool:
    """`<digits>:<base64url>` là bot token, KHÔNG phải số điện thoại.

    Telethon hỏi gộp "Please enter your phone (or bot token):" — dán nhầm bot
    token vào đó tạo ra session CỦA BOT. Bot bị Telegram chặn `get_dialogs`
    (BotMethodInvalidError) nên không bao giờ lấy được access_hash cho kênh,
    và worker chết với "Could not find the input entity for PeerChannel" —
    thông báo chỉ sang hướng sai (access_hash/TARGET_CHANNEL).
    """
    dau, _, than = s.strip().partition(":")
    return dau.isdigit() and bool(than)


async def main() -> None:
    api_id = int(input("API_ID: "))
    api_hash = input("API_HASH: ")
    phone = input(
        "So dien thoai (dinh dang +8490...), KHONG phai bot token: "
    ).strip()
    if la_bot_token(phone):
        raise SystemExit(
            "Bạn vừa dán BOT TOKEN, không phải số điện thoại.\n"
            "Session của bot KHÔNG dùng được: Telegram chặn get_dialogs "
            "(BotMethodInvalidError) nên worker không resolve được kênh.\n"
            "Chạy lại và nhập số điện thoại account THẬT (vd +84901234567).\n"
            "Bot token chỉ dùng cho BOT_TOKEN (gửi thông báo), tách biệt hoàn toàn."
        )
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        # Cùng fingerprint với worker (worker/uploader.py, runbook §3a) —
        # session đăng ký sạch ngay từ lúc login, notification "thiết bị mới"
        # hiển thị Desktop/Windows thay vì default Telethon (1.45.0/PC 64bit/kernel).
        device_model="Desktop",
        system_version="Windows 11 x64",
        app_version="7.2.8",
        lang_code="en",
        system_lang_code="en-US",
    )
    async with client:
        print("\nStringSession (dán vào Actions Secret TELETHON_SESSION):")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
