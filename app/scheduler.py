import logging
from datetime import datetime, timedelta

from pytz import timezone
from sqlalchemy import select

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
)

from app.db import AsyncSessionLocal
from app.models import ScheduleMessage, User

logger = logging.getLogger(__name__)

MSK = timezone("Europe/Moscow")


async def send_daily(bot, session_factory=AsyncSessionLocal):
    """
    Отправляет одно ежедневное сообщение (по дате) всем пользователям,
    у которых consent = true.
    """

    today_msk = datetime.now(MSK).date()
    now_utc = datetime.utcnow()

    async with session_factory() as session:

        # 1. Сообщение по расписанию на сегодня
        msg = await session.scalar(
            select(ScheduleMessage)
            .where(ScheduleMessage.send_date == today_msk)
            .where(ScheduleMessage.sent_at.is_(None))
        )

        if not msg:
            logger.info("send_daily: no scheduled message for %s", today_msk)
            return

        # 2. Все пользователи с consent
        users = (await session.scalars(
            select(User).where(User.consent.is_(True))
        )).all()

        if not users:
            logger.info("send_daily: no consenting users")
            return

        delivered = 0

        # 3. Отправка
        for user in users:
            try:
                logger.info(
                    "send_daily: sending msg_id=%s to user_id=%s chat_id=%s",
                    msg.id, user.id, user.tg_chat_id
                )

                await bot.send_message(
                    chat_id=user.tg_chat_id,
                    text=msg.text
                )

                delivered += 1

            except TelegramForbiddenError:
                logger.warning(
                    "send_daily: user blocked bot user_id=%s",
                    user.id
                )
                continue

            except TelegramNetworkError as exc:
                logger.warning(
                    "send_daily: network error user_id=%s err=%s",
                    user.id, exc
                )
                continue

            except Exception:
                logger.exception(
                    "send_daily: unexpected error user_id=%s",
                    user.id
                )
                continue

        # 4. Помечаем отправленным ТОЛЬКО если доставили хотя бы одному
        if delivered > 0:
            msg.sent_at = now_utc
            await session.commit()
            logger.info(
                "send_daily: message %s marked sent (%s users)",
                msg.id, delivered
            )
        else:
            logger.warning(
                "send_daily: message %s was not delivered to anyone",
                msg.id
            )


async def send_outbox(
    bot,
    session_factory=AsyncSessionLocal,
    batch_size: int = 20,
    retry_delay_seconds: int = 60,
):
    """
    Отправляет отложенные сообщения из ScheduleMessage (send_at).
    Поддерживает retry и логирование.
    """

    now_utc = datetime.utcnow()

    async with session_factory() as session:

        messages = (await session.scalars(
            select(ScheduleMessage)
            .where(ScheduleMessage.sent_at.is_(None))
            .where(ScheduleMessage.send_at.is_not(None))
            .where(ScheduleMessage.send_at <= now_utc)
            .order_by(ScheduleMessage.send_at, ScheduleMessage.id)
            .limit(batch_size)
        )).all()

        if not messages:
            logger.debug("send_outbox: no messages to send")
            return

        users = (await session.scalars(
            select(User).where(User.consent.is_(True)).order_by(User.id)
        )).all()

        if not users:
            logger.warning("send_outbox: no consenting users")
            for msg in messages:
                msg.send_at = now_utc + timedelta(seconds=retry_delay_seconds)
                msg.last_error = "NO_USERS"
                msg.last_attempt_at = now_utc
                msg.attempts = (msg.attempts or 0) + 1
            await session.commit()
            return

        for msg in messages:
            msg.attempts = (msg.attempts or 0) + 1
            msg.last_attempt_at = now_utc

            delivered = 0
            for user in users:
                try:
                    logger.info(
                        "send_outbox: sending schedule_id=%s to user_id=%s chat_id=%s",
                        msg.id, user.id, user.tg_chat_id
                    )

                    await bot.send_message(
                        chat_id=user.tg_chat_id,
                        text=msg.text
                    )
                    delivered += 1

                except TelegramForbiddenError:
                    logger.warning(
                        "send_outbox: user blocked bot user_id=%s schedule_id=%s",
                        user.id, msg.id
                    )
                    continue

                except TelegramNetworkError as exc:
                    logger.warning(
                        "send_outbox: network error schedule_id=%s user_id=%s err=%s",
                        msg.id, user.id, exc
                    )
                    continue

                except Exception:
                    logger.exception(
                        "send_outbox: unexpected error schedule_id=%s user_id=%s",
                        msg.id, user.id
                    )
                    continue

            if delivered > 0:
                msg.sent_at = now_utc
                msg.last_error = None
                logger.info(
                    "send_outbox: sent schedule_id=%s to %s users",
                    msg.id, delivered
                )
            else:
                msg.last_error = "NO_DELIVERY"
                msg.send_at = now_utc + timedelta(seconds=retry_delay_seconds)
                logger.warning(
                    "send_outbox: no deliveries schedule_id=%s",
                    msg.id
                )

        await session.commit()
