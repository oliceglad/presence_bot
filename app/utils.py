import logging
import traceback
import asyncio
from aiogram import Bot
from app.config import BOT_TOKEN, ADMIN_TG_ID

async def send_alert_async(text):
    if not ADMIN_TG_ID:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        async with bot.session:
            # Split long messages if needed, though for alerts usually short
            await bot.send_message(ADMIN_TG_ID, f"🚨 **ALERT** 🚨\n\n{text}"[:4096])
    except Exception as e:
        print(f"Failed to send alert: {e}")

def send_alert(text):
    """
    Synchronous wrapper to send alert (fire and forget task if loop running, or run run_until_complete).
    Taking a simpler approach: scheduling it on the current event loop if available.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send_alert_async(text))
    except RuntimeError:
        # No running loop, run strictly
        asyncio.run(send_alert_async(text))

class TelegramAlertHandler(logging.Handler):
    def emit(self, record):
        try:
            if record.levelno >= logging.ERROR:
                msg = self.format(record)
                # optionally include traceback if available
                if record.exc_info:
                    msg += "\n\n" + "".join(traceback.format_exception(*record.exc_info))
                
                send_alert(msg)
        except Exception:
            self.handleError(record)
