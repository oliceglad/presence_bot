import asyncio
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from app.config import BOT_TOKEN, SEND_HOUR, SEND_MINUTE, TIMEZONE, USE_CELERY, ENABLE_SCHEDULES
from app.handlers import router
from app.scheduler import send_daily, send_outbox

async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    if not USE_CELERY and ENABLE_SCHEDULES:
        scheduler = AsyncIOScheduler(timezone=timezone(TIMEZONE))
        scheduler.add_job(
            send_daily,
            "cron",
            hour=SEND_HOUR,
            minute=SEND_MINUTE,
            args=[bot]
        )
        scheduler.add_job(
            send_outbox,
            "interval",
            seconds=10,
            args=[bot]
        )
        scheduler.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
