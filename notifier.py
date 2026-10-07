"""
Отправка служебных сообщений бота из обычного (синхронного) кода — например, когда
синхронизация с Google Calendar обнаружила, что токен пользователя больше не работает.
Ошибки отправки логируются и никогда не ломают основную операцию.
"""
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
APP_URL = (os.environ.get("WEBHOOK_BASE_URL") or "https://julyera-agent.onrender.com").rstrip("/") + "/"


def send_telegram(chat_id: int, text: str, with_app_button: bool = False) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    payload = {"chat_id": chat_id, "text": text}
    if with_app_button:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "Открыть приложение", "web_app": {"url": APP_URL}}]]}
        )
    try:
        resp = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=payload, timeout=10)
        return resp.status_code == 200
    except Exception:
        logger.exception("Не удалось отправить сообщение в Telegram")
        return False


def notify_user(user_id: str, text: str, with_app_button: bool = False) -> int:
    """Шлёт сообщение во все чаты пользователя; возвращает, сколько отправлено."""
    import users_service

    sent = 0
    for chat_id in users_service.get_chat_ids(user_id):
        if send_telegram(chat_id, text, with_app_button):
            sent += 1
    return sent
