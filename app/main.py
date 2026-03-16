import asyncio
import logging
import time

import redis
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from app.utils import TelegramAlertHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        TelegramAlertHandler()
    ]
)

from app.config import (
    BOT_TOKEN,
    SEND_HOUR,
    SEND_MINUTE,
    REMINDER_HOUR,
    REMINDER_MINUTE,
    TIMEZONE,
    USE_CELERY,
    ENABLE_SCHEDULES,
    REDIS_URL,
)
from app.handlers import router
from app.scheduler import send_daily, send_outbox, send_reminders, send_future_messages

logger = logging.getLogger(__name__)

def redis_is_available(url: str, attempts: int = 5, delay_seconds: float = 1.0) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            client = redis.from_url(
                url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            return True
        except Exception as exc:
            logger.warning(
                "Redis not available (attempt %s/%s): %s",
                attempt,
                attempts,
                exc,
            )
            time.sleep(delay_seconds)
    return False

async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    use_celery = USE_CELERY
    if USE_CELERY and ENABLE_SCHEDULES and not redis_is_available(REDIS_URL):
        logger.warning("Redis is down; falling back to in-process scheduler.")
        use_celery = False

    if not use_celery and ENABLE_SCHEDULES:
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
        scheduler.add_job(
            send_future_messages,
            "cron",
            hour=SEND_HOUR,
            minute=SEND_MINUTE,
            args=[bot]
        )
        scheduler.add_job(
            send_reminders,
            "cron",
            hour=REMINDER_HOUR,
            minute=REMINDER_MINUTE,
            args=[bot]
        )
        scheduler.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
