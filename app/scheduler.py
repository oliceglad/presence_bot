import logging
from datetime import datetime, timedelta

from pytz import timezone
from sqlalchemy import select

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
)

from app.db import AsyncSessionLocal
from app.models import ScheduleMessage, User, Subscription
from app.config import (
    REMINDER_EXPIRES_IN_DAYS,
    REMINDER_INACTIVITY_DAYS,
    REMINDER_COOLDOWN_HOURS,
)

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


async def send_reminders(bot, session_factory=AsyncSessionLocal):
    """
    Отправляет напоминания о скором окончании подписки и бездействии.
    """
    now = datetime.utcnow()
    cooldown = timedelta(hours=REMINDER_COOLDOWN_HOURS)

    async with session_factory() as session:
        users = (await session.scalars(
            select(User).where(User.consent.is_(True)).order_by(User.id)
        )).all()

        if not users:
            logger.debug("send_reminders: no consenting users")
            return

        for user in users:
            if user.snooze_until and user.snooze_until > now:
                continue

            sub = await session.scalar(
                select(Subscription).where(Subscription.user_id == user.id)
            )

            lines = []
            update_expiry = False
            update_inactivity = False

            if sub and sub.expires_at:
                days_left = (sub.expires_at.date() - now.date()).days
                needs_expiry = days_left <= REMINDER_EXPIRES_IN_DAYS
                can_send_expiry = (
                    not user.last_expiry_reminder_at
                    or now - user.last_expiry_reminder_at >= cooldown
                )
                if needs_expiry and can_send_expiry:
                    if days_left < 0:
                        lines.append("Подписка закончилась. Пришли доказательство, чтобы продлить.")
                    elif days_left == 0:
                        lines.append("Подписка заканчивается сегодня. Пришли доказательство, чтобы продлить.")
                    elif days_left == 1:
                        lines.append("Подписка заканчивается завтра. Пришли доказательство, чтобы продлить.")
                    else:
                        lines.append(
                            f"Подписка заканчивается через {days_left} дн. Пришли доказательство, чтобы продлить."
                        )
                    update_expiry = True

            last_activity = user.last_activity_at or user.created_at
            if last_activity:
                inactive_days = (now.date() - last_activity.date()).days
                needs_inactive = inactive_days >= REMINDER_INACTIVITY_DAYS
                can_send_inactive = (
                    not user.last_inactivity_reminder_at
                    or now - user.last_inactivity_reminder_at >= cooldown
                )
                if needs_inactive and can_send_inactive:
                    lines.append(
                        f"Мы давно не виделись ({inactive_days} дн.). Напиши пару слов или пришли доказательство."
                    )
                    update_inactivity = True

            if not lines:
                continue

            try:
                await bot.send_message(user.tg_chat_id, "\n".join(lines))
            except TelegramForbiddenError:
                logger.warning("send_reminders: user blocked bot user_id=%s", user.id)
                continue
            except TelegramNetworkError as exc:
                logger.warning("send_reminders: network error user_id=%s err=%s", user.id, exc)
                continue
            except Exception:
                logger.exception("send_reminders: unexpected error user_id=%s", user.id)
                continue

            if update_expiry:
                user.last_expiry_reminder_at = now
            if update_inactivity:
                user.last_inactivity_reminder_at = now

        await session.commit()
