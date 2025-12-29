import asyncio
import csv
from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import ScheduleMessage, ActionRule


async def import_csv():
    async with AsyncSessionLocal() as session:
        exists = await session.scalar(select(ScheduleMessage))
        if exists:
            return

        with open("messages_365.csv", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                session.add(ScheduleMessage(
                    day_index=int(row["day_index"]),
                    send_date=datetime.fromisoformat(row["date"]).date(),
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
    await import_csv()
    await ensure_action_rules()


if __name__ == "__main__":
    asyncio.run(main())
