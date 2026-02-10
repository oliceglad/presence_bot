from datetime import datetime, timedelta, timezone
from app.tasks import send_broadcast_task

# Announcement text
TEXT = """Привет! 💖
У нас вышло большое обновление!

Что нового:
✅ **Режим "14 Февраля"** — задания, предсказания и романтика! (Кнопка в меню появится сама).
✅ **Магазин и Баллы** — теперь за выполнение заданий ты получаешь баллы. Их можно тратить на купоны (массаж, кино и др.)!
✅ **Удобное меню** — кнопки теперь всегда под рукой.
✅ **Исправления** — бот работает быстрее и стабильнее.

Заходи скорее и нажимай "💘 14 Февраля"! С праздником! 🥰"""

# Image path (inside container)
IMAGE_PATH = "/app/banner_val.png"

# Target time: Feb 11, 10:00 MSK (UTC+3) -> 07:00 UTC
# Alternatively, if server is UTC, we target 07:00 UTC.
# Note: MSK is UTC+3.
# 10:00 MSK = 07:00 UTC.

target_time = datetime(2026, 2, 11, 7, 0, 0, tzinfo=timezone.utc)
now = datetime.now(timezone.utc)

if target_time < now:
    print(f"Time {target_time} has passed! Scheduling for NOW + 1 minute for test.")
    eta = now + timedelta(minutes=1)
else:
    eta = target_time

print(f"Scheduling broadcast for: {eta} UTC")

# Queue the task
result = send_broadcast_task.apply_async(
    args=[TEXT, IMAGE_PATH],
    eta=eta
)

print(f"Task queued! ID: {result.id}")
