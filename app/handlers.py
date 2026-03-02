import csv
import io
import asyncio
import logging
import random
from typing import Optional
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
    Subscription,
    ActionRule,
    ActionEvent,
    ScheduleMessage,
    SupportMessage,
    Coupon,
    UserCoupon,
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
ADMIN_PENDING_BROADCAST = {} # admin_id -> {text, media_type, media_file_id}
ADMIN_PENDING_MESSAGE_USER_ID = {} # admin_id -> target_user_id
ADMIN_PENDING_POINTS = {} # admin_id -> {step: 'user_id'|'amount', user_id: int, amount: int}
COMPLIMENT_PAGE_SIZE = 10
COMPLIMENT_BUTTON_MAX = 48

class UserStates(StatesGroup):
    waiting_for_proof = State()
    waiting_for_march_proof = State()

TASKS = [
    "10 минут прогулки",
    "3 благодарности в дневнике",
    "15 минут растяжки",
    "30 минут без соцсетей",
    "сделала приятный сюрприз",
]

PROOF_HINT = "Нужны фото/кружок/видео."

def admin_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="История сообщений")],
            [KeyboardButton(text="Статус подписки"), KeyboardButton(text="Пользователь")],
            [KeyboardButton(text="Все 365 сообщений"), KeyboardButton(text="Сообщение на завтра")],
            [KeyboardButton(text="Изменить на завтра"), KeyboardButton(text="Отправить сегодня")],
            [KeyboardButton(text="Написать пользователю"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="💰 Начислить баллы")],
        ],
        resize_keyboard=True
    )

def user_reply_keyboard():
    rows = [
        [KeyboardButton(text="📋 Меню"), KeyboardButton(text="📤 Отправить отчет")],
        [KeyboardButton(text="💳 Подписка"), KeyboardButton(text="📖 Правила")],
        [KeyboardButton(text="🛍 Магазин"), KeyboardButton(text="🎒 Мои купоны"), KeyboardButton(text="🆘 Поддержка")],
    ]
    
    # Valentine's Season (Feb 11 - Feb 15)
    now = datetime.now()
    if now.month == 3:
        rows.insert(0, [KeyboardButton(text="🌸 Март")])
        
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def march_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💌 Задание дня"), KeyboardButton(text="📤 Отчет по заданию")],
            [KeyboardButton(text="🔮 Предсказание"), KeyboardButton(text="🔙 Назад")],
        ],
        resize_keyboard=True
    )

def march_admin_keyboard(inbox_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять (+5 баллов)", callback_data=f"march_quest:approve:{inbox_id}")],
        [InlineKeyboardButton(text="⛔️ Отклонить", callback_data=f"march_quest:deny:{inbox_id}")]
    ])

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
    if message.voice:
        return "voice", message.voice.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.document:
        return "document", message.document.file_id
    return None, None

def has_proof_media(message: Message) -> bool:
    return bool(message.photo or message.video or message.video_note or message.document)

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


def extract_sticker(message: Message):
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None, None

async def get_active_rules(session):
    return (await session.scalars(
        select(ActionRule)
        .where(ActionRule.active.is_(True))
        .order_by(ActionRule.id)
    )).all()

async def get_user_smart(session, user_id_val: int):
    # Attempt to find user by internal ID (PK) OR by Telegram User ID.
    # Postgres INTEGER (PK) max value is ~2.14 billion.
    # Telegram User IDs (BigInteger) can be much larger and will cause DataError if passed to session.get(User, val).
    
    INT32_MAX = 2_147_483_647
    
    # 1. If it's too large for int32, it MUST be a TG ID (or invalid PK)
    if user_id_val > INT32_MAX:
        return await session.scalar(select(User).where(User.tg_user_id == user_id_val))
    
    # 2. If it fits int32, it could be PK
    user = await session.get(User, user_id_val)
    if user:
        return user
        
    # 3. If not found by PK, maybe it's a small TG ID (older account)?
    return await session.scalar(select(User).where(User.tg_user_id == user_id_val))


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
            "Админ режим. Клавиатура обновлена.",
            reply_markup=admin_reply_keyboard()
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
        if sub and message.from_user.id != ADMIN_TG_ID:
            expires = sub.expires_at.strftime("%Y-%m-%d %H:%M")
            await message.bot.send_message(
                ADMIN_TG_ID,
                f"Старт подписки: до {expires}"
            )
        await message.answer(
            "Хорошо 🤍\n"
            "Это твой личный дневник. Я сохраняю все сообщения и действия.\n"
            "Чтобы продлевать подписку, отправляй фото/кружок/видео — я передам на проверку.\n"
            f"{PROOF_HINT}"
        )
        await message.answer(
            "Подписка действует 1 месяц и продлевается за действия.\n"
            "Для задания укажи, что сделала: /rules",
            reply_markup=user_reply_keyboard()
        )
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

    lines = [
        "📋 **Правила продления**",
        "Каждое действие продлевает подписку:",
    ]
    for rule in rules_list:
        lines.append(f"🔹 **{rule.title}**: +{rule.days_to_extend} дн.")
    
    lines.append("")
    lines.append("📸 **Как продлить:**")
    lines.append("Отправь фото, видео или кружок в этот чат. Админ проверит и подтвердит.")
    lines.append(PROOF_HINT)
    
    lines.append("")
    lines.append("💎 **Баллы и Магазин**")
    lines.append("✅ За каждое подтвержденное действие: **+10 баллов**!")
    lines.append("Трать баллы в **Магазине** на купоны (массаж, свидание и др.).")
    
    lines.append("")
    lines.append("🎒 **Купоны**")
    lines.append("Купленные купоны хранятся в меню '🎒 Мои купоны'.")
    
    lines.append("")
    lines.append("📝 **Идеи для заданий:**")
    for task in TASKS:
        lines.append(f"• {task}")
        
    await message.answer("\n".join(lines), parse_mode="Markdown")

@router.message(F.text == "/admin")
async def admin_menu(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await message.answer("Меню админа:", reply_markup=admin_reply_keyboard())




@router.message(F.text == "/menu")
async def user_menu_command(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        await message.answer("Меню админа:", reply_markup=admin_reply_keyboard())
        return
    await message.answer("Пользовательское меню:", reply_markup=user_reply_keyboard())

@router.message(F.text == "📋 Меню")
async def user_menu_text(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        return
    await message.answer("меню", reply_markup=user_reply_keyboard())

@router.message(F.text == "📖 Правила")
async def user_rules_text(message: Message):
    await rules(message)

@router.message(F.text == "💳 Подписка")
async def user_status_text(message: Message):
    await my_status(message)

@router.message(F.text == "🆘 Помощь")
async def user_help_text(message: Message):
    await help_command(message)


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

@router.message(F.text.in_({"/admin_user", "Пользователь"}))
async def admin_user(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_user_func(message.bot, message.chat.id)

async def send_admin_user_func(bot, chat_id: int):

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
        elif msg.media_type == "voice":
            await bot.send_voice(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "sticker":
            await bot.send_sticker(chat_id, msg.media_file_id)
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
        elif msg.media_type == "voice":
            await bot.send_voice(chat_id, msg.media_file_id, caption=msg.text)
        elif msg.media_type == "sticker":
            await bot.send_sticker(chat_id, msg.media_file_id)
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

async def send_admin_history(bot, chat_id: int):
    async with AsyncSessionLocal() as session:
        # Fetch sent messages
        sent_msgs = (await session.scalars(
            select(ScheduleMessage)
            .where(ScheduleMessage.sent_at.is_not(None))
            .order_by(desc(ScheduleMessage.sent_at))
            .limit(20)
        )).all()
    
    if not sent_msgs:
        await bot.send_message(chat_id, "История отправленных сообщений пуста.")
        return

    lines = ["Последние 20 отправленных сообщений:"]
    for msg in sent_msgs:
        when = msg.sent_at.strftime("%Y-%m-%d %H:%M")
        text_snippet = (msg.text or "")[:30].replace("\n", " ")
        lines.append(f"- {when}: {text_snippet}...")
    
    await bot.send_message(chat_id, "\n".join(lines))

@router.message(F.text == "История сообщений")
async def admin_history(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_history(message.bot, message.chat.id)

@router.message(F.text.in_({"/status", "Статус подписки"}))
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

@router.message(F.text.in_({"/outbox", "Сообщение на завтра"}))
async def outbox(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_next_message(message.bot, message.chat.id)

@router.message(F.text.in_({"/set_tomorrow", "Изменить на завтра"}))
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

@router.message(F.text.in_({"/schedule_all", "Все 365 сообщений"}))
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

@router.message(F.text.in_({"/send_daily_now", "Отправить сегодня"}))
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

@router.message(F.text == "/help")
async def help_command(message: Message):
    if message.from_user.id == ADMIN_TG_ID:
        await message.answer(
            "Команды админа:\n"
            "Кнопки меню внизу экрана.\n\n"
            "Доп. команды:\n"
            "/rules — правила продления\n"
            "/proofs — последние доказательства\n"
            "/send_compliment <day|id>\n"
            "/pick_compliment — выбрать комплимент\n"
            "/schedule_status\n\n"
            "Проверка доказательств: придет уведомление с кнопками."
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


async def send_support_keyboard(message: Message):
    async with AsyncSessionLocal() as session:
        items = (await session.scalars(select(SupportMessage))).all()

    if not items:
        await message.answer("Раздел поддержки пока пуст.")
        return

    # Create rows of buttons
    rows = []
    for item in items:
        rows.append([KeyboardButton(text=f"🆘 {item.title}")])
    rows.append([KeyboardButton(text="🔙 Назад")])

    kb = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
    await message.answer("Что случилось? Выбери вариант:", reply_markup=kb)


@router.message(F.text == "🆘 Поддержка")
async def user_support_menu(message: Message):
    await send_support_keyboard(message)

@router.message(F.text == "🔙 Назад")
async def back_to_main(message: Message):
    await message.answer("Главное меню", reply_markup=user_reply_keyboard())


@router.message(F.text.startswith("🆘 "))
async def support_content_handler(message: Message):
    title = message.text[2:] # strip "🆘 "
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(SupportMessage).where(SupportMessage.title == title))

    if not item:
        # Might be a mismatch or old button
        return

    # Send content
    chat_id = message.chat.id
    bot = message.bot
    try:
        if item.media_type == "sticker":
            await bot.send_sticker(chat_id, item.media_file_id)
        elif item.media_type == "photo":
            await bot.send_photo(chat_id, item.media_file_id, caption=item.text)
        elif item.media_type == "video":
            await bot.send_video(chat_id, item.media_file_id, caption=item.text)
        elif item.media_type == "voice":
            await bot.send_voice(chat_id, item.media_file_id, caption=item.text)
        elif item.text:
            await bot.send_message(chat_id, item.text)
        else:
            await bot.send_message(chat_id, "Сообщение поддержки.")
    except Exception:
        await bot.send_message(chat_id, "Ошибка при отправке поддержки.")


ADMIN_PENDING_SUPPORT = {} # admin_id -> {key, title}

@router.message(F.text.startswith("/add_support"))
async def add_support_start(message: Message):
    # Usage: /add_support <key> <Button Title>
    if message.from_user.id != ADMIN_TG_ID:
        return
    
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /add_support <key> <Текст кнопки>\nПример: /add_support sad Грустно")
        return
    
    key = parts[1]
    title = parts[2]
    
    ADMIN_PENDING_SUPPORT[message.from_user.id] = {"key": key, "title": title}
    await message.answer(f"Добавляем поддержку '{title}' (key={key}).\nПришли контент (текст/фото/видео/стикер/войс) или /cancel")

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_SUPPORT)
async def add_support_content(message: Message):
    if message.text == "/cancel":
        ADMIN_PENDING_SUPPORT.pop(message.from_user.id)
        await message.answer("Отменено.")
        return

    data = ADMIN_PENDING_SUPPORT.pop(message.from_user.id)
    key = data["key"]
    title = data["title"]
    
    content_text = message.text or message.caption
    media_type = None
    media_file_id = None
    
    if message.sticker:
        media_type = "sticker"
        media_file_id = message.sticker.file_id
    elif message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_file_id = message.video.file_id
    elif message.voice:
        media_type = "voice"
        media_file_id = message.voice.file_id
    
    async with AsyncSessionLocal() as session:
        # Check if exists
        existing = await session.scalar(select(SupportMessage).where(SupportMessage.key == key))
        if existing:
            existing.title = title
            existing.text = content_text
            existing.media_type = media_type
            existing.media_file_id = media_file_id
            await message.answer(f"Обновлено: {title}")
        else:
            session.add(SupportMessage(
                key=key,
                title=title,
                text=content_text,
                media_type=media_type,
                media_file_id=media_file_id
            ))
            await message.answer(f"Создано: {title}")
        await session.commit()


@router.message(F.text == "🛍 Магазин")
async def user_shop_menu(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_user_id == message.from_user.id))
        coupons = (await session.scalars(select(Coupon).where(Coupon.active.is_(True)))).all()

    points = user.points or 0
    text = f"🛍 Магазин желаний\nВаш баланс: {points} 💎\n\nВыберите купон:"
    
    rows = []
    if coupons:
        for coupon in coupons:
            rows.append([InlineKeyboardButton(
                text=f"{coupon.title} ({coupon.cost} 💎)",
                callback_data=f"buy_coupon:{coupon.id}"
            )])
    else:
        text += "\n(Купонов пока нет)"

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("buy_coupon:"))
async def buy_coupon_callback(callback: CallbackQuery):
    coupon_id = int(callback.data.split(":")[1])
    
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_user_id == callback.from_user.id))
        coupon = await session.get(Coupon, coupon_id)
        
        if not coupon or not coupon.active:
            await callback.answer("Купон недоступен.")
            return
        
        if (user.points or 0) < coupon.cost:
            await callback.answer("Недостаточно баллов.", show_alert=True)
            return
        
        # Deduct points
        user.points -= coupon.cost
        
        # Give coupon
        user_coupon = UserCoupon(user_id=user.id, coupon_id=coupon.id)
        session.add(user_coupon)
        await session.commit()
        
        coupon_title = coupon.title

    await callback.answer("Куплено!", show_alert=True)
    await callback.message.edit_text(f"Вы купили купон: {coupon_title}! 🎉\nНайти его можно в меню '🎒 Мои купоны'.")
    
    # Notify Admin
    await callback.message.bot.send_message(
        ADMIN_TG_ID,
        f"💰 Пользователь купил купон: {coupon_title} ({coupon.cost} баллов)."
    )


@router.message(F.text == "🎒 Мои купоны")
async def my_coupons_list(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_user_id == message.from_user.id))
        items = (await session.scalars(
            select(UserCoupon)
            .where(UserCoupon.user_id == user.id)
            .where(UserCoupon.status == "active")
            .order_by(UserCoupon.created_at.desc())
        )).all()
        
        # Need to fetch titles, simple way loop or join
        # For simplicity, let's just do a join if needed, or lazy load if configured. 
        # But async lazy load is tricky. Let's do a join query.
        result = await session.execute(
            select(UserCoupon, Coupon)
            .join(Coupon, UserCoupon.coupon_id == Coupon.id)
            .where(UserCoupon.user_id == user.id)
            .where(UserCoupon.status == "active")
            .order_by(UserCoupon.created_at.desc())
        )
        coupons = result.all()

    if not coupons:
        await message.answer("У вас нет активных купонов.")
        return

    text = "🎒 Ваши активные купоны:"
    rows = []
    for uc, c in coupons:
        rows.append([InlineKeyboardButton(
            text=f"Использовать: {c.title}",
            callback_data=f"use_coupon:{uc.id}"
        )])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("use_coupon:"))
async def use_coupon_callback(callback: CallbackQuery):
    uc_id = int(callback.data.split(":")[1])
    
    async with AsyncSessionLocal() as session:
        uc = await session.get(UserCoupon, uc_id)
        if not uc or uc.status != "active":
            await callback.answer("Купон уже использован или недействителен.")
            await clear_inline_keyboard(callback.message)
            return
        
        # Verify user owns it
        user = await session.scalar(select(User).where(User.tg_user_id == callback.from_user.id))
        if uc.user_id != user.id:
             await callback.answer("Ошибка доступа.")
             return

        coupon = await session.get(Coupon, uc.coupon_id)
        coupon_title = coupon.title if coupon else "Купон"

        uc.status = "used"
        uc.redeemed_at = datetime.utcnow()
        await session.commit()

    await callback.answer("Купон использован!", show_alert=True)
    await callback.message.edit_text(f"Вы использовали купон: {coupon_title}. \nАдмин уведомлен.")
    
    await callback.message.bot.send_message(
        ADMIN_TG_ID,
        f"🎫 Пользователь ИСПОЛЬЗОВАЛ купон: {coupon_title}!"
    )

ADMIN_PENDING_COUPON = {}

@router.message(F.text.startswith("/add_coupon"))
async def add_coupon_start(message: Message):
    # /add_coupon Title | Cost
    if message.from_user.id != ADMIN_TG_ID:
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
         await message.answer("Формат: /add_coupon Название | Стоимость")
         return
         
    content = parts[1]
    if "|" not in content:
         await message.answer("Разделите название и цену символом |")
         return
         
    title, cost_str = content.split("|", 1)
    try:
        cost = int(cost_str.strip())
    except ValueError:
        await message.answer("Цена должна быть числом.")
        return
        
    async with AsyncSessionLocal() as session:
        session.add(Coupon(title=title.strip(), cost=cost))
        await session.commit()
    
    await message.answer(f"Купон '{title.strip()}' за {cost} баллов создан.")


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

@router.message(F.text == "Написать пользователю")
async def ask_user_id_for_message(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    ADMIN_PENDING_MESSAGE_USER_ID[message.from_user.id] = None
    await message.answer("Введите ID пользователя (TG Chat ID) или перешлите сообщение от него.")

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_MESSAGE_USER_ID and ADMIN_PENDING_MESSAGE_USER_ID[m.from_user.id] is None)
async def receive_user_id_for_message(message: Message):
    target_id = None
    if message.forward_from:
        target_id = message.forward_from.id
    elif message.text and message.text.isdigit():
        target_id = int(message.text)
    
    if target_id:
        ADMIN_PENDING_MESSAGE_USER_ID[message.from_user.id] = target_id
        await message.answer(f"ID {target_id} принят. Введите сообщение для отправки (текст/фото/видео/стикер).")
    else:
        ADMIN_PENDING_MESSAGE_USER_ID.pop(message.from_user.id, None)
        await message.answer("Некорректный ID. Отмена.")

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_MESSAGE_USER_ID and ADMIN_PENDING_MESSAGE_USER_ID[m.from_user.id] is not None)
async def send_message_to_user(message: Message):
    target_id = ADMIN_PENDING_MESSAGE_USER_ID.pop(message.from_user.id)
    try:
        if message.sticker:
            await message.bot.send_sticker(target_id, message.sticker.file_id)
        elif message.photo:
            await message.bot.send_photo(target_id, message.photo[-1].file_id, caption=message.caption)
        elif message.video:
            await message.bot.send_video(target_id, message.video.file_id, caption=message.caption)
        elif message.voice:
            await message.bot.send_voice(target_id, message.voice.file_id, caption=message.caption)
        elif message.text:
            await message.bot.send_message(target_id, message.text)
        else:
             await message.bot.send_message(target_id, "Сообщение (формат не поддерживается).")
        
        await message.answer("Сообщение отправлено.")
    except Exception as e:
        await message.answer(f"Ошибка отправки: {e}")




@router.message(F.text == "/test_schedule")
async def test_schedule(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return

    await send_random_to_users(message.bot, message.chat.id)




# --- Valentine's Season Handlers ---

@router.message(F.text == "🌸 Март")
async def march_menu(message: Message):
    now = datetime.now()
    if not (now.month == 3):
        await message.answer("Март еще не наступил или уже прошел! 🥀", reply_markup=user_reply_keyboard())
        return
    await message.answer(
        "🌸 **Весенний марафон!**\n\nВыполняйте задания, получайте предсказания и копите баллы!", 
        reply_markup=march_reply_keyboard(),
        parse_mode="Markdown"
    )

@router.message(F.text == "🔙 Назад")
async def back_to_main(message: Message):
    await message.answer("Главное меню", reply_markup=user_reply_keyboard())

@router.message(F.text == "💌 Задание дня")
async def march_quest_handler(message: Message):
    now = datetime.now()
    day = now.day
    
    quests = {
        1: "🌸 **День 1: Начало весны**\\nСделай фото первого весеннего дня и отправь его мне!",
        2: "🎵 **День 2: Весенний ритм**\\nПришли 3 песни, которые создают весеннее настроение.",
        3: "☕️ **День 3: Уют**\\nВыпей любимый напиток и пришли фото кружки!",
        4: "🚶‍♀️ **День 4: Шаги**\\nПройди сегодня 5000 шагов и скинь скриншот шагомера.",
        5: "🌿 **День 5: Природа**\\nНайди что-то зеленое на улице и сфотографируй.",
        6: "📖 **День 6: Цитата**\\nНапиши мне свою самую любимую цитату.",
        7: "👗 **День 7: Образ**\\nОденься сегодня во что-то яркое и скинь селфи в зеркале.",
        8: "🌷 **День 8: Праздник**\\nПоздравь себя с 8 марта! Скинь аудиособщение с пожеланием себе.",
        9: "🌤 **День 9: Небо**\\nСфотографируй небо, какое оно сейчас.",
        10: "🧘‍♀️ **День 10: Релакс**\\nУдели 15 минут спокойствию. Пришли фото того, как ты отдыхаешь.",
        11: "🎨 **День 11: Творчество**\\nНарисуй что-нибудь (хоть смайлик) на листочке и сфоткай.",
        12: "🍽 **День 12: Вкуснотища**\\nСделай фото своего самого вкусного приема пищи за сегодня.",
        13: "🧹 **День 13: Чистота**\\nВыброси 3 ненужные вещи. Пришли фото, от чего избавилась.",
        14: "💖 **День 14: Любовь к себе**\\nНапиши 3 качества, которые ты в себе обожаешь.",
        15: "🎬 **День 15: Кино**\\nПосоветуй мне классный фильм на вечер.",
        16: "🐈 **День 16: Животные**\\nСфоткай любого котика, собачку или птичку на улице.",
        17: "👟 **День 17: Активность**\\nСделай 10 приседаний! Можешь скинуть кружочек (по желанию) или просто написать 'готово'.",
        18: "💄 **День 18: Красота**\\nСделай себе красивый макияж (или уход) и скинь селфи.",
        19: "📚 **День 19: Книги**\\nПокажи, какую книгу ты сейчас читаешь (или хотела бы прочитать).",
        20: "🌟 **День 20: Радость**\\nЧто сегодня заставило тебя улыбнуться? Расскажи аудиосообщением.",
        21: "🌳 **День 21: Парк**\\nПрогуляйся сегодня хотя бы 10 минут по парку или аллее. Скинь фото деревьев.",
        22: "🥤 **День 22: Вода**\\nВыпей стакан воды прямо сейчас и скинь фото стакана.",
        23: "📸 **День 23: Воспоминания**\\nНайди свое любимое летнее фото и скинь мне!",
        24: "🎁 **День 24: Подарок себе**\\nКупи себе любую приятную мелочь и покажи мне.",
        25: "📝 **День 25: Планы**\\nНапиши 3 главных плана на грядущее лето.",
        26: "🕯 **День 26: Вечер**\\nЗажги свечу или включи уютный свет. Скинь атмосферное фото.",
        27: "🎧 **День 27: Подкаст**\\nПослушай любой интересный подкаст или видео хотя бы 5 минут и напиши его название.",
        28: "🛍 **День 28: Вишлист**\\nПришли одну вещь, которую очень хочешь купить.",
        29: "🤸‍♀️ **День 29: Разминка**\\nПотянись хорошенько! Напиши 'Потянулась', когда сделаешь.",
        30: "💐 **День 30: Цветы**\\nКупи себе цветы или найди красивые на картинке и пришли.",
        31: "🎉 **День 31: Финал**\\nПоздравляю, март пройден! Напиши, какое задание было самым классным."
    }
    
    text = quests.get(day, "На сегодня заданий нет. Отдыхай! 💕")
    await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "📤 Отчет по заданию")
async def start_march_proof_submission(message: Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_user_id == message.from_user.id))
        if not user:
            return

        # Check for existing submissions today
        today = datetime.now().date()
        stmt = select(InboxMessage).where(
            InboxMessage.user_id == user.id,
            InboxMessage.action_status.in_(["approved_march", "pending"]), # Check approved or pending
            func.date(InboxMessage.created_at) == today
        )
        existing = await session.scalar(stmt)
        
        if existing:
            if existing.action_status == "approved_march":
                await message.answer("Ты уже выполнила задание сегодня! Умничка! Заходи завтра. 😘")
                return
            else:
                 # Allow multiple pending submissions in case user wants to change proof
                 # await message.answer("Твой отчет за сегодня уже на проверке! Жди вердикт. ⏳")
                 pass

    await state.set_state(UserStates.waiting_for_march_proof)
    await message.answer("Пришли фото или текст с выполненным заданием марта! 🌸")

@router.message(F.text == "🔮 Предсказание")
async def march_prediction(message: Message):
    predictions = [
        "Твоя улыбка сегодня растопит чье-то сердце! ❤️",
        "Жди приятный сюрприз вечером! 🎁",
        "Весна уже здесь... Вдохни поглубже! 🌸",
        "Ты — самое дорогое, что у него есть! 💎",
        "Сегодня идеальный день для обнимашек! 🤗",
        "Твои глаза сияют ярче звезд! ✨",
        "Скоро в твоей жизни случится что-то волшебное! 🪄",
        "Доверься своей интуиции сегодня, она не подведет! 🌙",
        "Впереди тебя ждут отличные новости! 💌",
        "Сегодняшний день принесет тебе много радости! ☀️",
        "Твоя энергия сегодня на высоте! Сверни горы! 🏔",
        "Кто-то думает о тебе прямо сейчас с улыбкой. 😊",
        "Разреши себе сегодня маленькую шалость! 🧁",
        "Все, за что ты сегодня возьмешься, получится легко! 🕊",
        "На этой неделе тебя ждет приятная встреча! ☕️",
        "Твоему обаянию сегодня невозможно сопротивляться! 💃",
        "Случайность сегодня обернется большой удачей! 🍀",
        "Послушай любимую песню, в ней есть подсказка для тебя! 🎧"
    ]
    await message.answer(f"🔮 {random.choice(predictions)}")


@router.message(F.text.in_({"/broadcast", "📢 Рассылка"}))
async def broadcast_start(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    ADMIN_PENDING_BROADCAST[message.from_user.id] = None
    await message.answer("📢 Введите сообщение для рассылки всем пользователям (текст/фото/видео).")


@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_BROADCAST and ADMIN_PENDING_BROADCAST[m.from_user.id] is None)
async def broadcast_content(message: Message):
    content_text = message.text or message.caption
    media_type = None
    media_file_id = None
    
    if message.sticker:
        media_type = "sticker"
        media_file_id = message.sticker.file_id
    elif message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_file_id = message.video.file_id
    elif message.voice:
        media_type = "voice"
        media_file_id = message.voice.file_id
        
    ADMIN_PENDING_BROADCAST[message.from_user.id] = {
        "text": content_text,
        "media_type": media_type,
        "media_file_id": media_file_id
    }
    
    # Preview
    await message.answer("Предпросмотр:")
    try:
        if media_type == "sticker":
            await message.bot.send_sticker(message.chat.id, media_file_id)
        elif media_type == "photo":
            await message.bot.send_photo(message.chat.id, media_file_id, caption=content_text)
        elif media_type == "video":
            await message.bot.send_video(message.chat.id, media_file_id, caption=content_text)
        elif media_type == "voice":
            await message.bot.send_voice(message.chat.id, media_file_id, caption=content_text)
        else:
             await message.answer(content_text or "[Пустое сообщение?]")
    except Exception as e:
        await message.answer(f"Ошибка предпросмотра: {e}")
        
    await message.answer(
        "Отправить всем пользователям?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✅ Отправить"), KeyboardButton(text="❌ Отмена")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_BROADCAST and m.text in ("✅ Отправить", "❌ Отмена"))
async def broadcast_confirm(message: Message):
    if message.text == "❌ Отмена":
        ADMIN_PENDING_BROADCAST.pop(message.from_user.id)
        await message.answer("Рассылка отменена.", reply_markup=admin_reply_keyboard())
        return

    data = ADMIN_PENDING_BROADCAST.pop(message.from_user.id)
    
    async with AsyncSessionLocal() as session:
        users = (await session.scalars(select(User))).all()
        
    sent_count = 0
    errors = 0
    
    status_msg = await message.answer(f"Начинаю рассылку на {len(users)} пользователей...")
    
    for user in users:
        try:
            if data["media_type"] == "sticker":
                await message.bot.send_sticker(user.tg_chat_id, data["media_file_id"])
            elif data["media_type"] == "photo":
                await message.bot.send_photo(user.tg_chat_id, data["media_file_id"], caption=data["text"])
            elif data["media_type"] == "video":
                await message.bot.send_video(user.tg_chat_id, data["media_file_id"], caption=data["text"])
            elif data["media_type"] == "voice":
                await message.bot.send_voice(user.tg_chat_id, data["media_file_id"], caption=data["text"])
            else:
                await message.bot.send_message(user.tg_chat_id, data["text"])
            sent_count += 1
        except Exception:
            errors += 1
            
    await status_msg.delete()
    await message.answer(
        f"✅ Рассылка завершена.\nОтправлено: {sent_count}\nОшибок: {errors}", 
        reply_markup=admin_reply_keyboard()
    )


@router.message(F.text.startswith("/add_points"))
async def add_points_command(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    
    parts = message.text.split()
    # Usage: /add_points <user_id> <amount>
    # If used as reply, user_id can be omitted: /add_points <amount>
    
    target_id = None
    amount = 0
    
    if message.reply_to_message:
        # Try to find user from reply
        pass 
        # Actually it's hard to get DB user id from reply unless we parse it or have it.
        # But we can try to guess or use the argument if provided.
    
    # Simpler logic: expect arguments
    if len(parts) < 3:
        await message.answer("Формат: /add_points <user_id> <amount>\nUser ID - это внутренний ID базы (не TG ID), можно узнать через 'Пользователь'.")
        return
        
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        await message.answer("ID и сумма должны быть числами.")
        return
        
    async with AsyncSessionLocal() as session:
        user = await get_user_smart(session, target_id)
        if not user:
            await message.answer(f"Пользователь с ID {target_id} не найден (ни по ID, ни по TG ID).")
            return
            
        user.points = (user.points or 0) + amount
        await session.commit()
        
        await message.answer(f"✅ Баланс пользователя {user.id} ({user.tg_user_id}) обновлен.\nБыло: {user.points - amount}\nСтало: {user.points}")
        
        try:
            await message.bot.send_message(user.tg_chat_id, f"🎉 Вам начислено {amount} баллов вручную админом! Ваш баланс: {user.points}")
        except Exception:
            await message.answer("Не удалось уведомить пользователя (блок бота?).")


@router.message(F.text == "💰 Начислить баллы")
async def add_points_ui_start(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    ADMIN_PENDING_POINTS[message.from_user.id] = {"step": "user_id"}
    await message.answer("Введите ID пользователя (внутренний ID или Telegram User ID).")

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_POINTS and ADMIN_PENDING_POINTS[m.from_user.id]["step"] == "user_id")
async def add_points_ui_user_id(message: Message):
    try:
        user_id = int(message.text)
    except ValueError:
        await message.answer("Нужен числовой ID. Отмена: /cancel")
        return

    ADMIN_PENDING_POINTS[message.from_user.id]["user_id"] = user_id
    ADMIN_PENDING_POINTS[message.from_user.id]["step"] = "amount"
    await message.answer(f"ID {user_id} принят. Введите сумму баллов для начисления:")

@router.message(lambda m: m.from_user.id == ADMIN_TG_ID and m.from_user.id in ADMIN_PENDING_POINTS and ADMIN_PENDING_POINTS[m.from_user.id]["step"] == "amount")
async def add_points_ui_amount(message: Message):
    try:
        amount = int(message.text)
    except ValueError:
        await message.answer("Нужно число. Отмена: /cancel")
        return

    data = ADMIN_PENDING_POINTS.pop(message.from_user.id)
    user_id_input = data["user_id"]

    async with AsyncSessionLocal() as session:
        user = await get_user_smart(session, user_id_input)
        
        if not user:
            await message.answer(f"Пользователь {user_id_input} не найден (ни по ID, ни по TG ID).")
            return
        
        user.points = (user.points or 0) + amount
        await session.commit()
        
        await message.answer(f"✅ Успешно! Пользователю {user.id} ({user.tg_user_id}) начислено {amount} баллов. Баланс: {user.points}")
        
        try:
            await message.bot.send_message(user.tg_chat_id, f"🎉 Вам начислено {amount} баллов вручную админом! Ваш баланс: {user.points}")
        except Exception:
            await message.answer("Не удалось уведомить пользователя.")


@router.message(F.text == "📤 Отправить отчет")
async def start_proof_submission(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_for_proof)
    await message.answer("Пожалуйста, отправь фото, видео или кружок сейчас. Это будет считаться отчетом для продления подписки.")

@router.message()
async def inbox(message: Message, state: FSMContext):
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
        
        current_state = await state.get_state()
        is_proof_mode = current_state == UserStates.waiting_for_proof.state
        is_march_proof = current_state == UserStates.waiting_for_march_proof.state
        
        has_proof = has_proof_media(message) and is_proof_mode
        
        
        now = datetime.utcnow()

        # Valentine proof handling (text or media)
        if is_march_proof:
            has_proof = True # treat everything as proof in this mode
        
        # Also catch implicit Valentine proofs via date if not in explicit mode but during season
        is_march_season = (now.month == 3)
        if is_march_season:
             # If user sends media during season, treat as potential proof even if not explicitly in mode
             if has_proof_media(message):
                 is_march_proof = True

            
        if has_proof or is_march_proof:
            await state.clear()
            
        inbox = InboxMessage(
            user_id=user.id,
            tg_message_id=message.message_id,
            text=text,
            media_type=media_type,
            media_file_id=media_file_id,
            # For val proof, we use a special status or just rely on the admin keyboard logic
            # Let's use "val_pending" to distinguish if needed, or just "pending" and context from admin message
            action_status="pending", 
            raw=message.model_dump_json(exclude_none=True, exclude_unset=True)
        )
        session.add(inbox)
        user.last_activity_at = now
        await session.flush() # get ID
        
        if is_march_proof:
            caption = f"🌸 Отчет (Март):\n{text or '[Медиа]'}"
            reply_markup = march_admin_keyboard(inbox.id)
            if media_type == "photo":
                await message.bot.send_photo(ADMIN_TG_ID, media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "video":
                await message.bot.send_video(ADMIN_TG_ID, media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "video_note":
                await message.bot.send_video_note(ADMIN_TG_ID, media_file_id, reply_markup=reply_markup)
            elif media_type == "voice":
                await message.bot.send_voice(ADMIN_TG_ID, media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "document":
                await message.bot.send_document(ADMIN_TG_ID, media_file_id, caption=caption, reply_markup=reply_markup)
            else:
                await message.bot.send_message(ADMIN_TG_ID, caption, reply_markup=reply_markup)
                
            await message.answer("Отчет отправлен на проверку! Жди вердикт. 🌸")
            await session.commit()
            return

        rules = []
        if has_proof and not is_march_proof:
            rules = await get_active_rules(session)

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
        if media_type == "video_note":
            await message.bot.send_video_note(
                ADMIN_TG_ID,
                media_file_id,
                reply_markup=admin_keyboard
            )
        elif media_type == "document":
             await message.bot.send_document(
                ADMIN_TG_ID,
                media_file_id,
                caption=caption,
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
        if media_type and media_file_id:
            if media_type == "photo":
                await message.bot.send_photo(ADMIN_TG_ID, media_file_id, caption=text or None)
            elif media_type == "video":
                await message.bot.send_video(ADMIN_TG_ID, media_file_id, caption=text or None)
            elif media_type == "video_note":
                await message.bot.send_video_note(ADMIN_TG_ID, media_file_id)
            elif media_type == "voice":
                await message.bot.send_voice(ADMIN_TG_ID, media_file_id, caption=text or None)
            elif media_type == "document":
                await message.bot.send_document(ADMIN_TG_ID, media_file_id, caption=text or None)
            else:
                await message.bot.send_message(
                    ADMIN_TG_ID,
                    f"Сообщение от неё:\n{text or '[медиа]'}"
                )
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

        # Award points
        async with AsyncSessionLocal() as session:
            user_model = await session.get(User, inbox.user_id)
            if user_model:
                user_model.points = (user_model.points or 0) + 10
                await session.commit()

        await clear_inline_keyboard(callback.message)
        await callback.answer("Продлено +10 баллов.")
        new_txt = new_expires.strftime("%Y-%m-%d %H:%M")
        await callback.message.answer(f"Подписка продлена до {new_txt}. Начислено 10 баллов.")
        if user_chat_id:
            await callback.message.bot.send_message(
                user_chat_id,
                f"Подписка продлена до {new_txt}. Спасибо за действие: {rule_title}!\nВам начислено 10 баллов! 🎉"
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


@router.callback_query(F.data.startswith("march_quest:"))
async def march_quest_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        return
        
    parts = callback.data.split(":")
    action = parts[1]
    inbox_id = int(parts[2])
    
    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox:
            await callback.answer("Не найдено")
            return
            
        user = await session.get(User, inbox.user_id)
        if not user:
            await callback.answer("Пользователь не найден")
            return
            
        if action == "approve":
            # Check if already approved today
            today = datetime.now().date()
            existing_approved = await session.scalar(
                select(InboxMessage).where(
                    InboxMessage.user_id == user.id,
                    InboxMessage.action_status == "approved_march",
                    func.date(InboxMessage.created_at) == today,
                    InboxMessage.id != inbox_id # exclude self if re-clicking
                )
            )
            
            if existing_approved:
                # Already paid for today, just mark this one as approved/duplicate
                inbox.action_status = "approved_march_duplicate"
                await session.commit()
                await callback.answer("Уже было одобрено сегодня. Баллы не начислены повторно.")
                await clear_inline_keyboard(callback.message)
                await callback.message.answer(f"✅ Дубликат принят (без баллов).")
                return

            if inbox.action_status == "approved_march":
                 await callback.answer("Уже принято.")
                 return

            user.points = (user.points or 0) + 5
            inbox.action_status = "approved_march"
            await session.commit()
            
            await callback.answer("Принято! +5 баллов")
            await clear_inline_keyboard(callback.message)
            await callback.message.answer(f"✅ Задание принято. Начислено 5 баллов.")
            
            await callback.message.bot.send_message(
                user.tg_chat_id,
                "🌸 Твой отчет принят! Тебе начислено +5 баллов! Ты умничка! 😘"
            )
        elif action == "deny":
            inbox.action_status = "denied_march"
            await session.commit()
            
            await callback.answer("Отклонено")
            await clear_inline_keyboard(callback.message)
            await callback.message.answer("⛔️ Задание отклонено.")
            
            await callback.message.bot.send_message(
                user.tg_chat_id,
                "💔 Твой отчет по заданию отклонен. Попробуй еще раз или уточни у админа."
            )




