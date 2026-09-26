"""
Локальный запуск Telegram-бота в режиме polling — для разработки и тестов.
Бот работает с базой напрямую через task_service, поэтому запущенный
backend (uvicorn) для этого больше не обязателен — нужны только
SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY и TELEGRAM_BOT_TOKEN в .env.

Для продакшена (Render) используется режим webhook внутри main.py —
там же и работает уже задеплоенный backend.

Запуск (в отдельном окне терминала):
    python bot.py
"""
import logging
import os

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters

load_dotenv()

from telegram_handlers import handle_text, handle_voice, list_tasks_handler, start

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN должен быть задан в .env файле. См. .env.example."
    )

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_tasks_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Бот запущен (polling), ждёт сообщений...")
    app.run_polling()


if __name__ == "__main__":
    main()
