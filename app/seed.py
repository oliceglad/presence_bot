import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import ScheduleMessage, ActionRule


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


if __name__ == "__main__":
    asyncio.run(main())
