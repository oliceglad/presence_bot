import csv
import io
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import select, desc, func
from pytz import timezone as pytz_timezone
from app.db import AsyncSessionLocal
from app.models import (
    User,
    InboxMessage,
    Subscription,
    ActionRule,
    ActionEvent,
    ScheduleMessage,
)
from app.config import (
    ADMIN_TG_ID,
    SUBSCRIPTION_START_DAYS,
    USE_CELERY,
    SEND_HOUR,
    SEND_MINUTE,
    TIMEZONE,
    REMINDER_SNOOZE_DEFAULT_DAYS,
    REMINDER_HOUR,
    REMINDER_MINUTE,
    ENABLE_SCHEDULES,
)
from app.tasks import send_random_task
from app.scheduler import send_daily

router = Router()
ADMIN_PENDING_TOMORROW = set()
ADMIN_PENDING_COMPLIMENT = set()
COMPLIMENT_PAGE_SIZE = 10
COMPLIMENT_BUTTON_MAX = 48

TASKS = [
    "10 минут прогулки",
    "3 благодарности в дневнике",
    "15 минут растяжки",
    "30 минут без соцсетей",
    "сделала приятный сюрприз",
]

PROOF_HINT = "Нужны фото/кружок/видео."

def admin_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Статус подписки", callback_data="admin:status")],
            [InlineKeyboardButton(text="Пользователь", callback_data="admin:user")],
            [InlineKeyboardButton(text="Все 365 сообщений", callback_data="admin:schedule")],
            [InlineKeyboardButton(text="Доказательства", callback_data="admin:proofs")],
            [InlineKeyboardButton(text="Сообщение на завтра", callback_data="admin:next")],
            [InlineKeyboardButton(text="Изменить сообщение на завтра", callback_data="admin:edit_next")],
            [InlineKeyboardButton(text="Случайное сообщение", callback_data="admin:random")],
            [InlineKeyboardButton(text="Выбрать комплимент", callback_data="admin:compliment")],
            [InlineKeyboardButton(text="Отправить сегодня", callback_data="admin:send_daily")],
            [InlineKeyboardButton(text="Статус расписания", callback_data="admin:schedule_status")],
            [InlineKeyboardButton(text="Комплимент по номеру", callback_data="admin:compliment_by_number")],
        ]
    )

def user_menu_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Меню", callback_data="user:menu")],
            [InlineKeyboardButton(text="📖 Правила", callback_data="user:rules")],
            [InlineKeyboardButton(text="💳 Подписка", callback_data="user:status")],
        ]
    )

def action_rules_keyboard(rules, inbox_id: int, prefix: str, include_deny: bool = False):
    rows = []
    for rule in rules:
        label = f"{rule.title} (+{rule.days_to_extend} дн.)"
        if prefix == "action_admin":
            callback_data = f"{prefix}:approve:{rule.id}:{inbox_id}"
        else:
            callback_data = f"{prefix}:{rule.id}:{inbox_id}"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback_data)])
    if include_deny:
        rows.append([InlineKeyboardButton(text="Отклонить", callback_data=f"{prefix}:deny:{inbox_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def clear_inline_keyboard(message: Message):
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        return

def extract_text(message: Message) -> str:
    return message.text or message.caption or ""

def extract_media(message: Message):
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return None, None

def has_proof_media(message: Message) -> bool:
    return bool(message.photo or message.video or message.video_note)

def shorten_text(text: str, max_len: int = COMPLIMENT_BUTTON_MAX) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[:max_len - 3]}..."

def compliments_keyboard(messages):
    rows = []
    for msg in messages:
        label = shorten_text(msg.text)
        if msg.day_index:
            label = f"{msg.day_index}: {label}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"compliment:send:{msg.id}",
        )])
    rows.append([InlineKeyboardButton(text="Еще", callback_data="compliment:next")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def parse_send_selector(raw: str):
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        return None, None
    if cleaned.startswith("id=") or cleaned.startswith("id:"):
        value = cleaned.split("=", 1)[1] if "id=" in cleaned else cleaned.split(":", 1)[1]
        return "id", value.strip()
    if cleaned.startswith("day=") or cleaned.startswith("day:"):
        value = cleaned.split("=", 1)[1] if "day=" in cleaned else cleaned.split(":", 1)[1]
        return "day", value.strip()
    return "day", cleaned

async def get_active_rules(session):
    return (await session.scalars(
        select(ActionRule)
        .where(ActionRule.active.is_(True))
        .order_by(ActionRule.id)
    )).all()

@router.message(F.text == "/start")
async def start(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        async with AsyncSessionLocal() as session:
            user = await session.scalar(
                select(User).where(User.tg_user_id == message.from_user.id)
            )
            if not user:
                user = User(
                    tg_user_id=message.from_user.id,
                    tg_chat_id=message.chat.id,
                    consent=True
                )
                session.add(user)
            else:
                user.consent = True
                user.tg_chat_id = message.chat.id
            await session.commit()
        await message.answer(
            "Админ режим. Доступны команды: /status, /rules, /test_schedule, /proofs, /help, /admin",
            reply_markup=admin_menu_keyboard()
        )
        return

    await message.answer(
        "Я сохраняю твои ответы в дневник, который видит Вячеслав. Ок?\n"
        "Ответь: Да или Нет."
    )

@router.message(F.text.casefold().in_(["да", "нет", "✅ да", "❌ нет"]))
async def consent(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == message.from_user.id)
        )
        if not user:
            user = User(
                tg_user_id=message.from_user.id,
                tg_chat_id=message.chat.id
            )
            session.add(user)

        user.consent = message.text.strip().lower() in ("да", "✅ да")
        sub = None
        if user.consent:
            sub = await session.scalar(
                select(Subscription).where(Subscription.user_id == user.id)
            )
            if not sub:
                now = datetime.utcnow()
                sub = Subscription(
                    user_id=user.id,
                    expires_at=now + timedelta(days=SUBSCRIPTION_START_DAYS)
                )
                session.add(sub)
        await session.commit()

    if message.text.strip().lower() in ("да", "✅ да"):
        if sub and sub.expires_at:
            expires = sub.expires_at.strftime("%Y-%m-%d %H:%M")
            await message.answer(f"Подписка активна до {expires}.")
        await message.answer(
            "Хорошо 🤍\n"
            "Это твой личный дневник. Я сохраняю все сообщения и действия.\n"
            "Чтобы продлевать подписку, отправляй фото/кружок/видео — я передам на проверку.\n"
            f"{PROOF_HINT}"
        )
        await message.answer(
            "Подписка действует 1 месяц и продлевается за действия.\n"
            "Для задания укажи, что сделала: /rules"
        )
        await message.answer("Пользовательское меню:", reply_markup=user_menu_inline_keyboard())
        if sub and message.from_user.id != ADMIN_TG_ID:
            expires = sub.expires_at.strftime("%Y-%m-%d %H:%M")
            await message.bot.send_message(
                ADMIN_TG_ID,
                f"Старт подписки: до {expires}"
            )
    else:
        await message.answer("Хорошо 🤍")

@router.message(F.text == "/rules")
async def rules(message: Message):
    async with AsyncSessionLocal() as session:
        rules_list = (await session.scalars(
            select(ActionRule)
            .where(ActionRule.active.is_(True))
            .order_by(ActionRule.id)
        )).all()

    if not rules_list:
        await message.answer("Правила пока не настроены.")
        return

    lines = ["Правила продления:"]
    for rule in rules_list:
        lines.append(f"- {rule.title}: +{rule.days_to_extend} дн.")
    lines.append(PROOF_HINT)
    lines.append("Как продлить: отправь фото/кружок/видео, админ подтвердит действие.")
    lines.append("После отправки выбери действие из списка.")
    lines.append("Задания:")
    for task in TASKS:
        lines.append(f"- {task}")
    await message.answer("\n".join(lines))

@router.message(F.text == "/admin")
async def admin_menu(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await message.answer("Меню админа:", reply_markup=admin_menu_keyboard())

@router.message(F.text == "/help")
async def help_command(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        await message.answer(
            "Команды админа:\n"
            "/status — статус подписки\n"
            "/rules — правила продления\n"
            "/proofs — последние доказательства\n"
            "/test_schedule — отправить случайное сообщение\n"
            "/send_random — отправить случайное сообщение\n"
            "/send_daily_now — отправить сообщение за сегодня вручную\n"
            "/send_compliment <day|id> — отправить комплимент по номеру дня или id\n"
            "/pick_compliment — выбрать и отправить комплимент вручную\n"
            "/schedule_status — показать текущие настройки расписания\n"
            "/schedule_all — все 365 сообщений\n"
            "/outbox — сообщение на завтра\n"
            "/set_tomorrow — изменить сообщение на завтра\n"
            "/admin — меню админа\n"
            "Проверка доказательств: выбери действие или «Отклонить» под медиа"
        )
    else:
        await message.answer(
            "Возможности бота:\n"
            "- сохраняет твои сообщения\n"
            "- продлевает подписку за действия\n"
            "- показывает правила: /rules\n"
            "- показывает статус подписки: /my_status\n"
            "- пауза напоминаний: /snooze 7, вернуть: /unsnooze\n"
            "Как продлить: отправь фото/кружок/видео, админ подтвердит действие.\n"
            "Открыть меню: /menu"
        )

@router.message(F.text == "/menu")
async def user_menu_command(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        await message.answer("Меню админа:", reply_markup=admin_menu_keyboard())
        return
    await message.answer("Пользовательское меню:", reply_markup=user_menu_inline_keyboard())


async def get_user_status_text(tg_user_id: int) -> str:
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == tg_user_id)
        )
        if not user:
            return "Профиль еще не создан."
        sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
    expires = sub.expires_at.strftime("%Y-%m-%d %H:%M") if sub and sub.expires_at else "нет"
    lines = [f"Подписка активна до: {expires}"]
    if user.snooze_until and user.snooze_until > datetime.utcnow():
        lines.append(f"Напоминания на паузе до: {user.snooze_until.strftime('%Y-%m-%d')}")
    return "\n".join(lines)


@router.message(F.text == "/my_status")
async def my_status(message: Message):
    text = await get_user_status_text(message.from_user.id)
    await message.answer(text)

@router.message(F.text.startswith("/snooze"))
async def snooze(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    days = REMINDER_SNOOZE_DEFAULT_DAYS
    if len(parts) > 1:
        try:
            days = int(parts[1])
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Укажи число дней, например: /snooze 7")
            return

    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == message.from_user.id)
        )
        if not user or not user.consent:
            await message.answer("Сначала /start.")
            return
        snooze_until = datetime.utcnow() + timedelta(days=days)
        user.snooze_until = snooze_until
        await session.commit()

    until_txt = snooze_until.strftime("%Y-%m-%d")
    await message.answer(f"Ок, напоминания на паузе до {until_txt}.")


@router.message(F.text == "/unsnooze")
async def unsnooze(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == message.from_user.id)
        )
        if not user or not user.consent:
            await message.answer("Сначала /start.")
            return
        user.snooze_until = None
        await session.commit()

    await message.answer("Напоминания снова активны.")

async def send_random_to_users(bot, chat_id: int):
    if USE_CELERY:
        send_random_task.delay()
        await bot.send_message(chat_id, "Задача отправки случайного сообщения поставлена.")
        return

    async with AsyncSessionLocal() as session:
        template_msg = await session.scalar(
            select(ScheduleMessage).order_by(func.random())
        )
        if not template_msg:
            await bot.send_message(chat_id, "В базе нет сообщений.")
            return

        users = (await session.scalars(
            select(User)
            .where(User.consent.is_(True))
            .where(User.tg_user_id != ADMIN_TG_ID)
            .order_by(User.id)
        )).all()
        if not users:
            await bot.send_message(chat_id, "Нет пользователей для отправки (нужен consent).")
            return

    delivered, total = await send_text_to_users(bot, template_msg.text)
    await bot.send_message(
        chat_id,
        f"Случайное сообщение отправлено: {delivered} из {total} пользователей."
    )

async def send_text_to_users(bot, text: str):
    async with AsyncSessionLocal() as session:
        users = (await session.scalars(
            select(User)
            .where(User.consent.is_(True))
            .where(User.tg_user_id != ADMIN_TG_ID)
            .order_by(User.id)
        )).all()
    if not users:
        return 0, 0

    delivered = 0
    for user in users:
        try:
            await bot.send_message(user.tg_chat_id, text)
            delivered += 1
        except TelegramNetworkError:
            continue
        except Exception:
            continue
    return delivered, len(users)

async def send_compliment_by_selector(bot, selector_type, selector_num):
    async with AsyncSessionLocal() as session:
        if selector_type == "id":
            msg = await session.get(ScheduleMessage, selector_num)
        else:
            msg = await session.scalar(
                select(ScheduleMessage).where(ScheduleMessage.day_index == selector_num)
            )
            if not msg:
                msg = await session.get(ScheduleMessage, selector_num)
    if not msg or not msg.text:
        return None, None
    return await send_text_to_users(bot, msg.text)

async def get_primary_user(session):
    return await session.scalar(select(User).order_by(User.id))

async def send_admin_status(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        user = await get_primary_user(session)
        if not user:
            await bot.send_message(chat_id, "Пользователя еще нет.")
            return
        sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
    expires = sub.expires_at.strftime("%Y-%m-%d %H:%M") if sub and sub.expires_at else "нет"
    await bot.send_message(chat_id, f"Подписка до: {expires}")

async def send_admin_user(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        user = await get_primary_user(session)
    if not user:
        await bot.send_message(chat_id, "Пользователя еще нет.")
        return
    await bot.send_message(
        chat_id,
        f"User id: {user.id}\nTG user id: {user.tg_user_id}\nChat id: {user.tg_chat_id}\nConsent: {user.consent}"
    )

async def send_admin_inbox(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        user = await get_primary_user(session)
        if not user:
            await bot.send_message(chat_id, "Пользователя еще нет.")
            return
        messages = (await session.scalars(
            select(InboxMessage)
            .where(InboxMessage.user_id == user.id)
            .order_by(InboxMessage.created_at.desc())
            .limit(10)
        )).all()
    if not messages:
        await bot.send_message(chat_id, "Сообщений пока нет.")
        return
    for msg in messages:
        if msg.media_type == "photo":
            await bot.send_photo(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "video":
            await bot.send_video(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "video_note":
            await bot.send_video_note(chat_id, msg.media_file_id)
        else:
            await bot.send_message(chat_id, msg.text or "[медиа]")

async def send_admin_proofs(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        user = await get_primary_user(session)
        if not user:
            await bot.send_message(chat_id, "Пользователя еще нет.")
            return
        proofs_list = (await session.scalars(
            select(InboxMessage)
            .where(InboxMessage.user_id == user.id)
            .where(InboxMessage.media_file_id.isnot(None))
            .order_by(InboxMessage.created_at.desc())
            .limit(10)
        )).all()
    if not proofs_list:
        await bot.send_message(chat_id, "Пока нет доказательств.")
        return
    for msg in proofs_list:
        if msg.media_type == "photo":
            await bot.send_photo(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "video":
            await bot.send_video(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "video_note":
            await bot.send_video_note(chat_id, msg.media_file_id)
        else:
            await bot.send_message(chat_id, msg.text or "[медиа]")

async def send_admin_schedule(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        items = (await session.scalars(
            select(ScheduleMessage)
            .order_by(ScheduleMessage.day_index)
        )).all()
    if not items:
        await bot.send_message(chat_id, "Сообщений в расписании нет.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["day_index", "send_date", "type", "text"])
    for item in items:
        writer.writerow([
            item.day_index,
            item.send_date.isoformat() if item.send_date else "",
            item.type or "",
            item.text or "",
        ])
    data = output.getvalue().encode("utf-8")
    await bot.send_document(chat_id, BufferedInputFile(data, filename="schedule_messages.csv"))

async def send_admin_next_message(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        tomorrow = datetime.now().date() + timedelta(days=1)
        msg = await session.scalar(
            select(ScheduleMessage)
            .where(ScheduleMessage.send_date == tomorrow)
            .where(ScheduleMessage.sent_at.is_(None))
        )
    if not msg:
        await bot.send_message(chat_id, "На завтра сообщений нет.")
        return
    time_txt = f"{SEND_HOUR:02d}:{SEND_MINUTE:02d} {TIMEZONE}"
    await bot.send_message(
        chat_id,
        f"Сообщение на завтра ({tomorrow} в {time_txt}):\n{msg.text}"
    )

async def update_admin_tomorrow_message(text: str):
    async with AsyncSessionLocal() as session:
        tomorrow = datetime.now().date() + timedelta(days=1)
        msg = await session.scalar(
            select(ScheduleMessage)
            .where(ScheduleMessage.send_date == tomorrow)
        )
        if msg:
            msg.text = text
            msg.type = msg.type or "manual"
            msg.sent_at = None
            msg.send_at = None
            msg.attempts = 0
            msg.last_attempt_at = None
            msg.last_error = None
        else:
            max_day_index = await session.scalar(
                select(func.max(ScheduleMessage.day_index))
            )
            session.add(ScheduleMessage(
                day_index=(max_day_index or 0) + 1,
                send_date=tomorrow,
                type="manual",
                text=text
            ))
        await session.commit()
        return tomorrow

@router.message(F.text == "/status")
async def status(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return

    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User))
        if not user:
            await message.answer("Пользователя еще нет.")
            return

        sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        result = await session.execute(
            select(ActionEvent, ActionRule)
            .join(ActionRule, ActionRule.id == ActionEvent.rule_id)
            .where(ActionEvent.user_id == user.id)
            .order_by(desc(ActionEvent.created_at))
            .limit(5)
        )
        events = result.all()

    expires = sub.expires_at.isoformat(sep=" ", timespec="minutes") if sub and sub.expires_at else "нет"
    lines = [f"Подписка до: {expires}"]
    if events:
        lines.append("Последние действия:")
        for ev, rule in events:
            when = ev.created_at.strftime("%Y-%m-%d %H:%M")
            new_exp = ev.new_expires_at.strftime("%Y-%m-%d %H:%M") if ev.new_expires_at else "нет"
            lines.append(f"- {when}: {rule.title} -> до {new_exp}")
    await message.answer("\n".join(lines))


@router.message(F.text == "/proofs")
async def proofs(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_proofs(message.bot, message.chat.id)

@router.message(F.text == "/outbox")
async def outbox(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_next_message(message.bot, message.chat.id)

@router.message(F.text == "/set_tomorrow")
async def set_tomorrow(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    ADMIN_PENDING_TOMORROW.add(message.from_user.id)
    await message.answer("Пришли новый текст для завтрашнего сообщения. Отмена: /cancel_tomorrow")

@router.message(F.text == "/cancel_tomorrow")
async def cancel_tomorrow(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    if message.from_user.id in ADMIN_PENDING_TOMORROW:
        ADMIN_PENDING_TOMORROW.discard(message.from_user.id)
        await message.answer("Отменено.")
    else:
        await message.answer("Нет активного редактирования.")

@router.message(F.text == "/cancel_compliment")
async def cancel_compliment(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    if message.from_user.id in ADMIN_PENDING_COMPLIMENT:
        ADMIN_PENDING_COMPLIMENT.discard(message.from_user.id)
        await message.answer("Отменено.")
    else:
        await message.answer("Нет активного выбора комплимента.")

@router.message(F.text == "/schedule_all")
async def schedule_all(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_schedule(message.bot, message.chat.id)

@router.message(F.text == "/schedule_status")
async def schedule_status(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return

    tz = pytz_timezone(TIMEZONE)
    now_local = datetime.now(tz)
    today_local = now_local.date()

    async with AsyncSessionLocal() as session:
        next_msg = await session.scalar(
            select(ScheduleMessage)
            .where(ScheduleMessage.send_date >= today_local)
            .where(ScheduleMessage.sent_at.is_(None))
            .order_by(ScheduleMessage.send_date)
        )

    lines = [
        f"Сейчас: {now_local.strftime('%Y-%m-%d %H:%M')} {TIMEZONE}",
        f"Дневная рассылка: {SEND_HOUR:02d}:{SEND_MINUTE:02d} {TIMEZONE}",
        f"Напоминания: {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} {TIMEZONE}",
        f"USE_CELERY={int(USE_CELERY)} ENABLE_SCHEDULES={int(ENABLE_SCHEDULES)}",
    ]
    if next_msg and next_msg.send_date:
        lines.append(f"Следующее сообщение: {next_msg.send_date.isoformat()}")
    else:
        lines.append("Следующее сообщение: нет")
    await message.answer("\n".join(lines))

@router.message(F.text == "/send_random")
async def send_random(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_random_to_users(message.bot, message.chat.id)

@router.message(F.text == "/send_daily_now")
async def send_daily_now(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_daily(message.bot)
    await message.answer("Попытался отправить дневное сообщение.")

@router.message(F.text.startswith("/send_compliment"))
async def send_compliment(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи номер дня или id, например: /send_compliment 25 или /send_compliment id=123")
        return

    selector_type, selector_value = parse_send_selector(parts[1])
    try:
        selector_num = int(selector_value)
    except (TypeError, ValueError):
        await message.answer("Нужно число, например: /send_compliment 25 или /send_compliment id=123")
        return

    delivered, total = await send_compliment_by_selector(message.bot, selector_type, selector_num)
    if delivered is None:
        await message.answer("Сообщение не найдено.")
        return

    await message.answer(f"Отправлено: {delivered} из {total} пользователей.")

@router.message(F.text == "/pick_compliment")
async def pick_compliment(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    async with AsyncSessionLocal() as session:
        messages = (await session.scalars(
            select(ScheduleMessage)
            .order_by(func.random())
            .limit(COMPLIMENT_PAGE_SIZE)
        )).all()
    if not messages:
        await message.answer("В базе нет сообщений.")
        return
    await message.answer(
        "Выбери комплимент для отправки:",
        reply_markup=compliments_keyboard(messages),
    )

@router.message()
async def inbox(message: Message):
    if message.from_user.id == ADMIN_TG_ID and message.from_user.id in ADMIN_PENDING_COMPLIMENT:
        text = extract_text(message).strip()
        if not text:
            await message.answer("Нужен номер дня или id. Отмена: /cancel_compliment")
            return
        ADMIN_PENDING_COMPLIMENT.discard(message.from_user.id)
        selector_type, selector_value = parse_send_selector(text)
        try:
            selector_num = int(selector_value)
        except (TypeError, ValueError):
            await message.answer("Нужно число, например: 25 или id=123")
            return
        delivered, total = await send_compliment_by_selector(message.bot, selector_type, selector_num)
        if delivered is None:
            await message.answer("Сообщение не найдено.")
            return
        await message.answer(f"Отправлено: {delivered} из {total} пользователей.")
        return

    if message.from_user.id == ADMIN_TG_ID and message.from_user.id in ADMIN_PENDING_TOMORROW:
        text = extract_text(message).strip()
        if not text:
            await message.answer("Нужен текст. Отмена: /cancel_tomorrow")
            return
        ADMIN_PENDING_TOMORROW.discard(message.from_user.id)
        tomorrow = await update_admin_tomorrow_message(text)
        await message.answer(f"Обновил сообщение на завтра ({tomorrow}).")
        return

    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == message.from_user.id)
        )
        if not user or not user.consent:
            return

        text = extract_text(message)
        media_type, media_file_id = extract_media(message)
        has_proof = has_proof_media(message)
        now = datetime.utcnow()
        inbox = InboxMessage(
            user_id=user.id,
            tg_message_id=message.message_id,
            text=text,
            media_type=media_type,
            media_file_id=media_file_id,
            action_status="pending" if has_proof else None,
            raw=message.model_dump_json()
        )
        session.add(inbox)
        user.last_activity_at = now

        rules = []
        if has_proof:
            rules = await get_active_rules(session)

        await session.flush()
        await session.commit()

    # уведомляем админа и даем кнопки выбора правила
    if has_proof:
        caption = f"Доказательство:\n{text}\n\nВыбери действие или отклони.".strip()
        admin_keyboard = action_rules_keyboard(rules, inbox.id, "action_admin", include_deny=True)
        if media_type == "photo":
            await message.bot.send_photo(
                ADMIN_TG_ID,
                media_file_id,
                caption=caption,
                reply_markup=admin_keyboard
            )
        elif media_type == "video":
            await message.bot.send_video(
                ADMIN_TG_ID,
                media_file_id,
                caption=caption,
                reply_markup=admin_keyboard
            )
        elif media_type == "video_note":
            await message.bot.send_video_note(
                ADMIN_TG_ID,
                media_file_id,
                reply_markup=admin_keyboard
            )
        else:
            await message.bot.send_message(
                ADMIN_TG_ID,
                caption,
                reply_markup=admin_keyboard
            )
        if rules:
            await message.answer(
                "Спасибо! Выбери действие для этого доказательства:",
                reply_markup=action_rules_keyboard(rules, inbox.id, "action_user")
            )
        else:
            await message.answer("Спасибо! Я передал доказательства на проверку.")
    else:
        await message.bot.send_message(
            ADMIN_TG_ID,
            f"Сообщение от неё:\n{text or '[медиа]'}"
        )


async def apply_action_for_inbox(inbox_id: int, rule_id: int):
    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox or inbox.user_id is None:
            return None, None, None, None

        if inbox.action_status in ("approved", "denied"):
            return "already", None, None, None

        user = await session.scalar(select(User).where(User.id == inbox.user_id))
        if not user:
            return None, None, None, None

        rule = await session.get(ActionRule, rule_id)
        if not rule or not rule.active:
            return None, None, None, None

        now = datetime.utcnow()
        sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        if not sub:
            sub = Subscription(user_id=user.id, expires_at=now)
            session.add(sub)
            await session.flush()

        old_expires = sub.expires_at
        base = old_expires if old_expires and old_expires > now else now
        new_expires = base + timedelta(days=rule.days_to_extend)
        sub.expires_at = new_expires

        raw = inbox.text or ""
        if rule.title:
            raw = f"{rule.title}; {raw}".strip("; ").strip()

        session.add(ActionEvent(
            user_id=user.id,
            rule_id=rule.id,
            raw_text=raw,
            old_expires_at=old_expires,
            new_expires_at=new_expires
        ))
        inbox.action_rule_id = rule.id
        inbox.action_status = "approved"
        inbox.action_reviewed_at = now
        await session.commit()

        return old_expires, new_expires, user.tg_chat_id, rule.title


async def deny_action_for_inbox(inbox_id: int):
    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox or inbox.user_id is None:
            return None, None

        if inbox.action_status in ("approved", "denied"):
            return "already", None

        user = await session.scalar(select(User).where(User.id == inbox.user_id))
        if not user:
            return None, None

        inbox.action_status = "denied"
        inbox.action_reviewed_at = datetime.utcnow()
        await session.commit()

        return user.tg_chat_id, user.tg_user_id


@router.callback_query(F.data.startswith("action_user:"))
async def action_user_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка данных.")
        return

    _, rule_id, inbox_id = parts
    try:
        rule_id = int(rule_id)
        inbox_id = int(inbox_id)
    except ValueError:
        await callback.answer("Ошибка данных.")
        return

    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox or inbox.user_id is None:
            await callback.answer("Сообщение не найдено.")
            return
        if inbox.action_status in ("approved", "denied"):
            await callback.answer("Уже обработано.")
            await clear_inline_keyboard(callback.message)
            return

        user = await session.scalar(select(User).where(User.id == inbox.user_id))
        if not user or user.tg_user_id != callback.from_user.id:
            await callback.answer("Недоступно.")
            return

        rule = await session.get(ActionRule, rule_id)
        if not rule or not rule.active:
            await callback.answer("Правило недоступно.")
            return

        rule_title = rule.title
        inbox.action_rule_id = rule.id
        if not inbox.action_status:
            inbox.action_status = "pending"
        await session.commit()

    await clear_inline_keyboard(callback.message)
    await callback.answer("Выбрано.")
    await callback.message.answer(f"Действие выбрано: {rule_title}.")
    await callback.message.bot.send_message(
        ADMIN_TG_ID,
        f"Пользователь выбрал действие: {rule_title} (доказательство #{inbox_id})."
    )


@router.callback_query(F.data.startswith("action_admin:"))
async def action_admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        await callback.answer("Недоступно.")
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return

    action = parts[1]
    if action == "approve":
        if len(parts) != 4:
            await callback.answer("Ошибка данных.")
            return
        _, _, rule_id, inbox_id = parts
        try:
            rule_id = int(rule_id)
            inbox_id = int(inbox_id)
        except ValueError:
            await callback.answer("Ошибка данных.")
            return

        result = await apply_action_for_inbox(inbox_id, rule_id)
        if result[0] == "already":
            await callback.answer("Уже обработано.")
            await clear_inline_keyboard(callback.message)
            return

        _, new_expires, user_chat_id, rule_title = result
        if not new_expires:
            await callback.answer("Не удалось применить действие.")
            return

        await clear_inline_keyboard(callback.message)
        await callback.answer("Продлено.")
        new_txt = new_expires.strftime("%Y-%m-%d %H:%M")
        await callback.message.answer(f"Подписка продлена до {new_txt}.")
        if user_chat_id:
            await callback.message.bot.send_message(
                user_chat_id,
                f"Подписка продлена до {new_txt}. Спасибо за действие: {rule_title}!"
            )
    elif action == "deny":
        if len(parts) != 3:
            await callback.answer("Ошибка данных.")
            return
        _, _, inbox_id = parts
        try:
            inbox_id = int(inbox_id)
        except ValueError:
            await callback.answer("Ошибка данных.")
            return

        user_chat_id, user_tg_id = await deny_action_for_inbox(inbox_id)
        if user_chat_id == "already":
            await callback.answer("Уже обработано.")
            await clear_inline_keyboard(callback.message)
            return

        await clear_inline_keyboard(callback.message)
        await callback.answer("Отклонено.")
        await callback.message.answer("Доказательство отклонено.")
        if user_chat_id:
            await callback.message.bot.send_message(
                user_chat_id,
                "Доказательство отклонено. Если есть ошибка, пришли еще раз."
            )
    else:
        await callback.answer("Неизвестно.")
        return




@router.callback_query(F.data.startswith("admin:"))
async def admin_menu_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        await callback.answer("Недоступно.")
        return

    action = callback.data.split(":", 1)[1]
    if action == "menu":
        await callback.message.answer("Меню админа:", reply_markup=admin_menu_keyboard())
    elif action == "rules":
        await rules(callback.message)
    elif action == "subscription":
        await send_admin_status(callback.message.bot, callback.message.chat.id)
    elif action == "status":
        await send_admin_status(callback.message.bot, callback.message.chat.id)
    elif action == "user":
        await send_admin_user(callback.message.bot, callback.message.chat.id)
    elif action == "inbox":
        await callback.message.answer("Раздел «Последние сообщения» отключен.")
    elif action == "proofs":
        await send_admin_proofs(callback.message.bot, callback.message.chat.id)
    elif action in ("next", "outbox"):
        await send_admin_next_message(callback.message.bot, callback.message.chat.id)
    elif action == "edit_next":
        ADMIN_PENDING_TOMORROW.add(callback.from_user.id)
        await callback.message.answer("Пришли новый текст для завтрашнего сообщения. Отмена: /cancel_tomorrow")
    elif action == "schedule":
        await send_admin_schedule(callback.message.bot, callback.message.chat.id)
    elif action == "send_daily":
        await send_daily(callback.message.bot)
        await callback.message.answer("Попытался отправить дневное сообщение.")
    elif action == "schedule_status":
        await schedule_status(callback.message)
    elif action == "compliment_by_number":
        ADMIN_PENDING_COMPLIMENT.add(callback.from_user.id)
        await callback.message.answer(
            "Пришли номер дня или id сообщения (например: 25 или id=123). Отмена: /cancel_compliment"
        )
    elif action == "reset":
        await callback.message.answer("Сброс дат отключен.")
    elif action in ("random", "test"):
        await send_random_to_users(callback.message.bot, callback.message.chat.id)
    elif action == "compliment":
        await pick_compliment(callback.message)
    else:
        await callback.answer("Неизвестно.")
        return

    await callback.answer()

@router.callback_query(F.data.startswith("compliment:"))
async def compliment_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        await callback.answer("Недоступно.")
        return

    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("Ошибка данных.")
        return

    action = parts[1]
    if action == "next":
        async with AsyncSessionLocal() as session:
            messages = (await session.scalars(
                select(ScheduleMessage)
                .order_by(func.random())
                .limit(COMPLIMENT_PAGE_SIZE)
            )).all()
        if not messages:
            await callback.answer("В базе нет сообщений.")
            return
        try:
            await callback.message.edit_text(
                "Выбери комплимент для отправки:",
                reply_markup=compliments_keyboard(messages),
            )
        except Exception:
            await callback.message.answer(
                "Выбери комплимент для отправки:",
                reply_markup=compliments_keyboard(messages),
            )
        await callback.answer()
        return

    if action != "send" or len(parts) != 3:
        await callback.answer("Ошибка данных.")
        return

    try:
        msg_id = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка данных.")
        return

    async with AsyncSessionLocal() as session:
        msg = await session.get(ScheduleMessage, msg_id)
    if not msg or not msg.text:
        await callback.answer("Сообщение не найдено.")
        return

    delivered, total = await send_text_to_users(callback.message.bot, msg.text)
    await callback.message.answer(
        f"Отправлено: {delivered} из {total} пользователей."
    )
    await callback.answer("Готово.")

@router.callback_query(F.data.startswith("user:"))
async def user_menu_callback(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    if action == "rules":
        await rules(callback.message)
    elif action == "status":
        text = await get_user_status_text(callback.from_user.id)
        await callback.message.answer(text)
    elif action == "menu":
        await callback.message.answer("Пользовательское меню:", reply_markup=user_menu_inline_keyboard())
    else:
        await callback.answer("Неизвестно.")
        return

    await callback.answer()


@router.message(F.text == "/test_schedule")
async def test_schedule(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return

    await send_random_to_users(message.bot, message.chat.id)
