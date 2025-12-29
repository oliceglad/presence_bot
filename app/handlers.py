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
)
from app.tasks import send_random_task

router = Router()

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
            [InlineKeyboardButton(text="Случайное сообщение", callback_data="admin:random")],
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

def approve_keyboard(inbox_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Док-ва присланы", callback_data=f"approve:{inbox_id}")]
        ]
    )

def approve_action_keyboard(inbox_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить", callback_data=f"approve_action:confirm:{inbox_id}")]
        ]
    )

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
            "/schedule_all — все 365 сообщений\n"
            "/outbox — сообщение на завтра\n"
            "/admin — меню админа\n"
            "Проверка доказательств: кнопка «Док-ва присланы» под медиа"
        )
    else:
        await message.answer(
            "Возможности бота:\n"
            "- сохраняет твои сообщения\n"
            "- продлевает подписку за действия\n"
            "- показывает правила: /rules\n"
            "- показывает статус подписки: /my_status\n"
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
    return f"Подписка активна до: {expires}"


@router.message(F.text == "/my_status")
async def my_status(message: Message):
    text = await get_user_status_text(message.from_user.id)
    await message.answer(text)

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

    delivered = 0
    for user in users:
        try:
            await bot.send_message(user.tg_chat_id, template_msg.text)
            delivered += 1
        except TelegramNetworkError:
            continue
        except Exception:
            continue

    await bot.send_message(
        chat_id,
        f"Случайное сообщение отправлено: {delivered} пользователям."
    )

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

@router.message(F.text == "/schedule_all")
async def schedule_all(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_admin_schedule(message.bot, message.chat.id)

@router.message(F.text == "/send_random")
async def send_random(message: Message):
    if message.from_user.id != ADMIN_TG_ID:
        return
    await send_random_to_users(message.bot, message.chat.id)

@router.message()
async def inbox(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.tg_user_id == message.from_user.id)
        )
        if not user or not user.consent:
            return

        text = extract_text(message)
        media_type, media_file_id = extract_media(message)
        inbox = InboxMessage(
            user_id=user.id,
            tg_message_id=message.message_id,
            text=text,
            media_type=media_type,
            media_file_id=media_file_id,
            raw=message.model_dump_json()
        )
        session.add(inbox)

        has_proof = has_proof_media(message)

        await session.commit()

    # уведомляем админа и даем кнопку подтверждения
    if has_proof:
        caption = f"Доказательство:\n{text}\n\nНажми «Док-ва присланы» для подтверждения.".strip()
        if media_type == "photo":
            await message.bot.send_photo(
                ADMIN_TG_ID,
                media_file_id,
                caption=caption,
                reply_markup=approve_keyboard(inbox.id)
            )
        elif media_type == "video":
            await message.bot.send_video(
                ADMIN_TG_ID,
                media_file_id,
                caption=caption,
                reply_markup=approve_keyboard(inbox.id)
            )
        elif media_type == "video_note":
            await message.bot.send_video_note(
                ADMIN_TG_ID,
                media_file_id,
                reply_markup=approve_keyboard(inbox.id)
            )
        else:
            await message.bot.send_message(
                ADMIN_TG_ID,
                caption,
                reply_markup=approve_keyboard(inbox.id)
            )
        await message.answer("Спасибо! Я передал доказательства на проверку.")
    else:
        await message.bot.send_message(
            ADMIN_TG_ID,
            f"Сообщение от неё:\n{text or '[медиа]'}"
        )


@router.callback_query(F.data.startswith("approve:"))
async def approve_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        await callback.answer("Недоступно.")
        return

    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("Ошибка данных.")
        return
    _, inbox_id = parts
    try:
        inbox_id = int(inbox_id)
    except ValueError:
        await callback.answer("Ошибка данных.")
        return

    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox or inbox.user_id is None:
            await callback.answer("Сообщение не найдено.")
            return
        if not inbox.media_file_id:
            await callback.answer("Нет медиа.")
            return

    await callback.message.edit_reply_markup(reply_markup=approve_action_keyboard(inbox_id))
    await callback.answer("Подтвердите действие.")


async def apply_action_for_inbox(inbox_id: int, action_key: str, task_name: str | None = None):
    async with AsyncSessionLocal() as session:
        inbox = await session.get(InboxMessage, inbox_id)
        if not inbox or inbox.user_id is None:
            return None, None, None

        user = await session.scalar(select(User).where(User.id == inbox.user_id))
        if not user:
            return None, None, None

        rule = await session.scalar(
            select(ActionRule)
            .where(ActionRule.key == action_key)
            .where(ActionRule.active.is_(True))
        )
        if not rule:
            return None, None, None

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
        if task_name:
            raw = f"{task_name}; {raw}".strip("; ").strip()

        session.add(ActionEvent(
            user_id=user.id,
            rule_id=rule.id,
            raw_text=raw,
            old_expires_at=old_expires,
            new_expires_at=new_expires
        ))
        await session.commit()

        return old_expires, new_expires, user.tg_chat_id


@router.callback_query(F.data.startswith("approve_action:"))
async def approve_action_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_TG_ID:
        await callback.answer("Недоступно.")
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка данных.")
        return
    _, action_key, inbox_id = parts
    try:
        inbox_id = int(inbox_id)
    except ValueError:
        await callback.answer("Ошибка данных.")
        return

    if action_key != "confirm":
        await callback.answer("Неверное действие.")
        return

    old_expires, new_expires, user_chat_id = await apply_action_for_inbox(inbox_id, "task")
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
            f"Подписка продлена до {new_txt}. Спасибо за действие!"
        )




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
    elif action == "schedule":
        await send_admin_schedule(callback.message.bot, callback.message.chat.id)
    elif action == "reset":
        await callback.message.answer("Сброс дат отключен.")
    elif action in ("random", "test"):
        await send_random_to_users(callback.message.bot, callback.message.chat.id)
    else:
        await callback.answer("Неизвестно.")
        return

    await callback.answer()


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
