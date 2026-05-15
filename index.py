import asyncio
import logging
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    from bot import register_handlers
    register_handlers(dp, bot)

    import database as db_module
    from cf_monitor import CFMonitor

    monitor = CFMonitor(
        bot=bot,
        get_all_users_fn=db_module.get_all_users_with_cookies,
        update_cookies_fn=db_module.update_user_cookies,
        alert_chat_ids=ADMIN_IDS,
        check_interval=60,
        retry_interval=120,
    )

    async def keepalive_all():
        from ivasms import IVASMSClient
        while True:
            await asyncio.sleep(1200)
            users = db_module.get_all_users_with_cookies()
            for uid, cookies in users.items():
                try:
                    async with IVASMSClient(cookies) as client:
                        await client.keepalive()
                except Exception as e:
                    logging.getLogger("keepalive_all").error(f"uid {uid}: {e}")

    await asyncio.gather(
        monitor.run(),
        keepalive_all(),
        dp.start_polling(bot, skip_updates=True),
    )


if __name__ == "__main__":
    asyncio.run(main())
