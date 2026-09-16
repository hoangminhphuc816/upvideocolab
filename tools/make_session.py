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


async def main() -> None:
    api_id = int(input("API_ID: "))
    api_hash = input("API_HASH: ")
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
