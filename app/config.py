import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_TG_ID = int(os.getenv("ADMIN_TG_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
SUBSCRIPTION_START_DAYS = int(os.getenv("SUBSCRIPTION_START_DAYS", "30"))
TIMEZONE = "Europe/Moscow"
SEND_HOUR = int(os.getenv("SEND_HOUR", "13"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
USE_CELERY = os.getenv("USE_CELERY", "0") == "1"
ENABLE_SCHEDULES = os.getenv("ENABLE_SCHEDULES", "0") == "1"
