"""
Хендлеры Telegram-бота: используются и для локального polling (bot.py),
и для продакшен-webhook (main.py). Работают с задачами напрямую через
task_service — без HTTP-круга внутри процесса.
"""
import logging
from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

import task_service

logger = logging.getLogger(__name__)

PRIORITY_EMOJI = {"High": "🔴", "Mid": "🟡", "Low": "🟢"}
CATEGORY_EMOJI = {"Study": "📚", "Work": "💼", "Personal": "🧍", "UniMe": "🎓"}


def _format_deadline(deadline_str: str | None) -> str:
    """Дата+время дедлайна в человекочитаемом виде: 'сегодня в 18:00' и т.п."""
    if not deadline_str:
        return "без дедлайна"
    dt = datetime.fromisoformat(deadline_str)
    today = date.today()
    days = (dt.date() - today).days
    time_part = dt.strftime("%H:%M")
    if days == 0:
        return f"сегодня в {time_part}"
    if days == 1:
        return f"завтра в {time_part}"
    if days < 0:
        return f"просрочено ({dt.strftime('%d.%m')} в {time_part})"
    return f"{dt.strftime('%d.%m')} в {time_part}"


def _format_task_line(task: dict) -> str:
    emoji_c = CATEGORY_EMOJI.get(task.get("category"), "")
    emoji_p = PRIORITY_EMOJI.get(task.get("priority"), "")
    deadline = _format_deadline(task.get("deadline"))
    return f"{emoji_c} {emoji_p} {task['task']} — {deadline}"


def format_reminder_message(tasks: list[dict]) -> str:
    """Сообщение для проактивного напоминания — с человекочитаемыми датой и временем."""
    lines = ["⏰ Напоминание о дедлайнах:"]
    for t in tasks:
        label = _format_deadline(t.get("deadline"))
        emoji_c = CATEGORY_EMOJI.get(t.get("category"), "")
        emoji_p = PRIORITY_EMOJI.get(t.get("priority"), "")
        lines.append(f"{emoji_c} {emoji_p} {t['task']} — {label}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_service.upsert_telegram_chat(update.effective_chat.id)
    await update.message.reply_text(
        "Привет! Просто напиши мне текст с задачей (или несколькими) — "
        "я сам разберу его на структурированные задачи и сохраню.\n\n"
        "Команды:\n"
        "/list — показать активные задачи (To Do)\n"
        "/start — это сообщение"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_service.upsert_telegram_chat(update.effective_chat.id)
    raw_text = update.message.text
    await update.message.chat.send_action("typing")

    try:
        tasks = task_service.create_tasks_from_text(raw_text)
    except Exception:
        logger.exception("Ошибка при разборе текста")
        await update.message.reply_text(
            "Не получилось разобрать текст 😕 Попробуй переформулировать."
        )
        return

    if not tasks:
        await update.message.reply_text("Не удалось извлечь ни одной задачи из текста.")
        return

    lines = ["Добавил:"] + [_format_task_line(t) for t in tasks]
    await update.message.reply_text("\n".join(lines))


async def list_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tasks = task_service.list_tasks(status="To Do")
    except Exception:
        logger.exception("Ошибка при получении списка задач")
        await update.message.reply_text("Не получилось получить список задач.")
        return

    if not tasks:
        await update.message.reply_text("Активных задач нет 🎉")
        return

    lines = ["Активные задачи:"] + [_format_task_line(t) for t in tasks]
    await update.message.reply_text("\n".join(lines))
