"""
One-time Telegram session setup.

Authenticates using your existing bot token — no phone number or code needed.
Run once before using the Telegram channel source:
    .venv/bin/python3 setup_telegram.py
"""
import asyncio
from pathlib import Path


async def main() -> None:
    try:
        from telethon import TelegramClient
    except ImportError:
        print("telethon not installed. Run: pip install telethon")
        return

    from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_TOKEN

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env first.")
        print("Get them from https://my.telegram.org → API development tools")
        return

    if not TELEGRAM_TOKEN:
        print("Set TELEGRAM_TOKEN in your .env first.")
        return

    session_file = str(Path(__file__).parent / "telegram_session")
    client = TelegramClient(session_file, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start(bot_token=TELEGRAM_TOKEN)
    me = await client.get_me()
    print(f"\nLogged in as bot: @{me.username}")
    print(f"Session saved to: {session_file}.session")
    print("\nYou can now run the scraper — Telegram channels will be included.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
