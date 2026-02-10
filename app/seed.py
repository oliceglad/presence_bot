import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import ScheduleMessage, ActionRule, SupportMessage, Coupon


def resolve_csv_path(path: str | None) -> Path:
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend([
        Path("messages_365.csv"),
        Path("app/messages_365.csv"),
    ])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("messages_365.csv not found")


def read_csv_rows(csv_path: Path):
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


async def import_csv_if_empty(csv_path: Path):
    async with AsyncSessionLocal() as session:
        exists = await session.scalar(select(ScheduleMessage))
        if exists:
            return

        for row in read_csv_rows(csv_path):
            session.add(ScheduleMessage(
                day_index=int(row["day_index"]),
                send_date=datetime.fromisoformat(row["date"]).date(),
                type=row["type"],
                text=row["text"]
            ))
        await session.commit()


async def upsert_csv(csv_path: Path):
    async with AsyncSessionLocal() as session:
        for row in read_csv_rows(csv_path):
            day_index = int(row["day_index"])
            send_date = datetime.fromisoformat(row["date"]).date()
            msg = await session.scalar(
                select(ScheduleMessage).where(ScheduleMessage.day_index == day_index)
            )
            if msg:
                msg.send_date = send_date
                msg.type = row["type"]
                msg.text = row["text"]
            else:
                session.add(ScheduleMessage(
                    day_index=day_index,
                    send_date=send_date,
                    type=row["type"],
                    text=row["text"]
                ))
        await session.commit()


async def ensure_action_rules():
    async with AsyncSessionLocal() as session:
        rules_by_key = {
            "smile": ("🙂 Улыбнулась", 30),
            "circle": ("🎥 Записала кружок", 30),
            "kiss": ("💋 Поцеловала", 30),
            "task": ("📝 Выполнила задание", 30),
        }
        existing = (await session.scalars(select(ActionRule))).all()
        existing_by_key = {r.key: r for r in existing}

        for key, (title, days) in rules_by_key.items():
            if key in existing_by_key:
                rule = existing_by_key[key]
                rule.title = title
                rule.days_to_extend = days
                rule.active = True
            else:
                session.add(ActionRule(key=key, title=title, days_to_extend=days))

        await session.commit()


async def ensure_support_messages():
    async with AsyncSessionLocal() as session:
        # Default support messages
        defaults = [
            {"key": "sad", "title": "Грустно", "text": "Не грусти, малыш! Я всегда с тобой. Обнимаю крепко-крепко! ❤️\nПомни, что ты у меня самая лучшая."},
            {"key": "tired", "title": "Устала", "text": "Ты большая умница и очень много делаешь. Отдохни немного, выпей чаю ☕️ и расслабься."},
            {"key": "bored", "title": "Скучно", "text": "Может быть, посмотрим кино? Или напиши мне, я всегда рад поболтать! 😉"},
            {"key": "miss", "title": "Скучаю", "text": "Я тоже очень скучаю по тебе! Скоро мы увидимся. Люблю тебя бесконечно! 💕"},
        ]
        
        for data in defaults:
            existing = await session.scalar(select(SupportMessage).where(SupportMessage.key == data["key"]))
            if not existing:
                session.add(SupportMessage(
                    key=data["key"],
                    title=data["title"],
                    text=data["text"],
                    media_type="text",
                    media_file_id=None
                ))
        await session.commit()


async def ensure_default_coupons():
    async with AsyncSessionLocal() as session:
        defaults = [
            {"title": "💆‍♀️ Массаж от меня (30 мин)", "cost": 50},
            {"title": "🍿 Киновечер (ты выбираешь фильм)", "cost": 30},
            {"title": "🍫 Вкусняшка", "cost": 10},
            {"title": "🍽 Ужин который приготовлю я", "cost": 70},
            {"title": "❤️ Любое желание (в пределах разумного)", "cost": 150},
        ]

        for data in defaults:
            existing = await session.scalar(select(Coupon).where(Coupon.title == data["title"]))
            if not existing:
                session.add(Coupon(
                    title=data["title"],
                    cost=data["cost"],
                    active=True
                ))
        
        await session.commit()


async def main():
    parser = argparse.ArgumentParser(description="Seed and sync schedule messages.")
    parser.add_argument(
        "--csv",
        default=None,
        help="Path to messages_365.csv (default: messages_365.csv or app/messages_365.csv)",
    )
    parser.add_argument(
        "--update-csv",
        action="store_true",
        help="Update existing schedule_messages from CSV (upsert by day_index).",
    )
    args = parser.parse_args()

    csv_path = resolve_csv_path(args.csv)

    if args.update_csv:
        await upsert_csv(csv_path)
    else:
        await import_csv_if_empty(csv_path)
    
    await ensure_action_rules()
    await ensure_support_messages()
    await ensure_default_coupons()


if __name__ == "__main__":
    asyncio.run(main())
