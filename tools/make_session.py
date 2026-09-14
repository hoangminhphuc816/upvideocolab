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
    client = TelegramClient(StringSession(), api_id, api_hash)
    async with client:
        print("\nStringSession (dán vào Actions Secret TELETHON_SESSION):")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
