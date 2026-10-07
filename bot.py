"""
Локальный запуск Telegram-бота в режиме polling — для разработки и тестов.
Бот работает с базой напрямую через task_service, поэтому запущенный
backend (uvicorn) для этого больше не обязателен — нужны только
SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY и TELEGRAM_BOT_TOKEN в .env.

Для продакшена (Render) используется режим webhook внутри main.py —
там же и работает уже задеплоенный backend. Polling и webhook у одного бота
несовместимы: запуск этого файла снимает webhook у прод-бота.

Запуск (в отдельном окне терминала):
    python bot.py
"""
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application

load_dotenv()

from telegram_handlers import register_handlers

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
    register_handlers(app)  # те же обработчики, что и в webhook (main.py), включая одобрение доступа
    logger.info("Бот запущен (polling), ждёт сообщений...")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
