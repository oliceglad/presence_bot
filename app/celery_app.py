from celery import Celery
from celery.schedules import crontab

from app.config import REDIS_URL, TIMEZONE, SEND_HOUR, SEND_MINUTE, ENABLE_SCHEDULES

celery_app = Celery(
    "presence_bot",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.timezone = TIMEZONE
celery_app.conf.enable_utc = False
celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.beat_schedule = {}
if ENABLE_SCHEDULES:
    celery_app.conf.beat_schedule = {
        "send-daily": {
            "task": "app.tasks.send_daily_task",
            "schedule": crontab(hour=SEND_HOUR, minute=SEND_MINUTE),
        },
    }
