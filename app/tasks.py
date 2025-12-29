import asyncio

from aiogram import Bot
import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.celery_app import celery_app
from app.config import BOT_TOKEN, DATABASE_URL, ADMIN_TG_ID
from app.scheduler import send_daily, send_outbox, send_reminders
from app.models import User, ScheduleMessage

logger = logging.getLogger(__name__)


async def _run_with_bot(coro):
    bot = Bot(BOT_TOKEN)
    try:
        await coro(bot)
    finally:
        await bot.session.close()

async def _run_with_bot_and_db(coro):
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    bot = Bot(BOT_TOKEN)
    try:
        await coro(bot, session_factory=session_factory)
    finally:
        await bot.session.close()
        await engine.dispose()


@celery_app.task(name="app.tasks.send_daily_task")
def send_daily_task():
    asyncio.run(_run_with_bot_and_db(send_daily))


@celery_app.task(name="app.tasks.send_outbox_task")
def send_outbox_task():
    asyncio.run(_run_with_bot_and_db(send_outbox))


@celery_app.task(name="app.tasks.send_reminders_task")
def send_reminders_task():
    asyncio.run(_run_with_bot_and_db(send_reminders))


async def send_random(bot, session_factory):
    async with session_factory() as session:
        template_msg = await session.scalar(
            select(ScheduleMessage).order_by(func.random())
        )
        if not template_msg:
            logger.warning("send_random: no messages in schedule_messages")
            return 0

        users = (await session.scalars(
            select(User)
            .where(User.consent.is_(True))
            .where(User.tg_user_id != ADMIN_TG_ID)
            .order_by(User.id)
        )).all()
        if not users:
            logger.warning("send_random: no consenting users")
            return 0

    delivered = 0
    for user in users:
        try:
            await bot.send_message(user.tg_chat_id, template_msg.text)
            delivered += 1
        except Exception:
            continue
    logger.info("send_random: delivered to %s users", delivered)
    return delivered


@celery_app.task(name="app.tasks.send_random_task")
def send_random_task():
    asyncio.run(_run_with_bot_and_db(send_random))
