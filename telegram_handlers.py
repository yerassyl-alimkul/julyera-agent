"""
Хендлеры Telegram-бота: используются и для локального polling (bot.py),
и для продакшен-webhook (main.py). Работают с задачами напрямую через
task_service — без HTTP-круга внутри процесса.

Доступ: пользоваться ботом и Mini App может только тот, кого владелец (is_admin)
разрешил кнопкой в ответ на /start. Неизвестный Telegram-аккаунт ничего не создаёт.
"""
import logging
import os
import time
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from groq_client import transcribe_audio
import categories_service
import task_service
import users_service

logger = logging.getLogger(__name__)

# Адрес приложения для кнопки Mini App и ссылки /web (на проде совпадает с WEBHOOK_BASE_URL).
APP_URL = (os.environ.get("WEBHOOK_BASE_URL") or "https://julyera-agent.onrender.com").rstrip("/") + "/"

PRIORITY_EMOJI = {"High": "🔴", "Mid": "🟡", "Low": "🟢"}

# Не больше одного запроса доступа в час от одного Telegram-id (в памяти процесса:
# после перезапуска сервиса счётчик сбрасывается, для двух человек этого достаточно).
ACCESS_REQUEST_COOLDOWN = 3600
_last_access_request: dict[int, float] = {}

NO_ACCESS_TEXT = "Нет доступа. Напиши /start, чтобы попросить доступ у владельца."


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
    """Строка задачи: приоритет, текст, название категории пользователя, дедлайн."""
    emoji_p = PRIORITY_EMOJI.get(task.get("priority"), "")
    deadline = _format_deadline(task.get("deadline"))
    return f"{emoji_p} {task['task']} · {task.get('category', '')} — {deadline}"


def format_reminder_message(tasks: list[dict]) -> str:
    """Сообщение для проактивного напоминания — с человекочитаемыми датой и временем."""
    lines = ["⏰ Напоминание о дедлайнах:"]
    lines += [_format_task_line(t) for t in tasks]
    return "\n".join(lines)


def open_app_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=APP_URL))]])


def _known_user(update: Update) -> dict | None:
    """Пользователь по Telegram-id отправителя (в личном чате он совпадает с chat_id).
    Известному пользователю заодно запоминаем чат для напоминаний."""
    if not update.effective_user:
        return None
    user = users_service.get_by_telegram_id(update.effective_user.id)
    if user and update.effective_chat:
        users_service.link_chat(update.effective_chat.id, user["id"])
    return user


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if user:
        await update.message.reply_text(
            f"Привет, {user['name']}! Напиши мне задачу текстом или голосом — я разберу её и сохраню.\n\n"
            "Команды:\n"
            "/list — активные задачи\n"
            "/categories — твои категории\n"
            "/web — личная ссылка, чтобы открыть приложение в браузере\n"
            "/start — это сообщение",
            reply_markup=open_app_markup(),
        )
        return

    tg_user = update.effective_user
    now = time.time()
    last = _last_access_request.get(tg_user.id)
    if last and now - last < ACCESS_REQUEST_COOLDOWN:
        await update.message.reply_text("Запрос уже отправлен владельцу, ждём ответа.")
        return
    _last_access_request[tg_user.id] = now

    name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or "Без имени"
    username = f" (@{tg_user.username})" if tg_user.username else ""
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Разрешить", callback_data=f"approve:{tg_user.id}"),
        InlineKeyboardButton("Отклонить", callback_data=f"deny:{tg_user.id}"),
    ]])
    sent = 0
    for admin in users_service.list_admins():
        if not admin.get("telegram_user_id"):
            continue
        try:
            await context.bot.send_message(
                chat_id=admin["telegram_user_id"],
                text=f"Запрос доступа: {name}{username}, Telegram-id {tg_user.id}.",
                reply_markup=markup,
            )
            sent += 1
        except Exception:
            logger.exception("Не удалось отправить запрос доступа владельцу")
    if not sent:
        _last_access_request.pop(tg_user.id, None)
        await update.message.reply_text("Не получилось связаться с владельцем, попробуй позже.")
        return
    await update.message.reply_text("Запрос отправлен владельцу. Как только он разрешит доступ, я пришлю кнопку.")


async def access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие «Разрешить» / «Отклонить» под запросом доступа."""
    query = update.callback_query
    # Решает только админ: проверяем того, кто нажал (from_user), а не данные кнопки.
    admin = users_service.get_by_telegram_id(query.from_user.id)
    if not users_service.is_admin(admin):
        await query.answer("Решение может принять только владелец.", show_alert=True)
        return

    try:
        action, raw_id = query.data.split(":", 1)
        telegram_user_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    original = (query.message.text if query.message else "") or "Запрос доступа"
    original = original.split("\n\n")[0]

    if action == "deny":
        if users_service.get_by_telegram_id(telegram_user_id):
            await query.answer("Этот человек уже допущен.", show_alert=True)
            return
        await query.answer("Отклонено")
        await query.edit_message_text(f"{original}\n\n❌ Отклонено")
        try:
            await context.bot.send_message(chat_id=telegram_user_id, text="Владелец отклонил запрос доступа.")
        except Exception:
            logger.exception("Не удалось сообщить об отказе")
        return

    if action != "approve":
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    # Повторное нажатие ничего не дублирует: пользователь с этим Telegram-id уже есть.
    existing = users_service.get_by_telegram_id(telegram_user_id)
    if existing:
        await query.answer("Уже разрешено.")
        await query.edit_message_text(f"{original}\n\n✅ Разрешено")
        return

    name = None
    try:
        chat = await context.bot.get_chat(telegram_user_id)
        name = " ".join(filter(None, [chat.first_name, chat.last_name])) or None
    except Exception:
        logger.exception("Не удалось получить имя нового пользователя")
    user = users_service.create_user(name, telegram_user_id)
    users_service.link_chat(telegram_user_id, user["id"])  # в личном чате chat_id = Telegram-id
    categories_service.ensure_default(user["id"])  # у каждого всегда есть хотя бы «Общее»
    _last_access_request.pop(telegram_user_id, None)

    await query.answer("Доступ открыт")
    await query.edit_message_text(f"{original}\n\n✅ Разрешено")
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text="Владелец открыл доступ. Пиши задачи сюда или открой приложение кнопкой ниже.",
            reply_markup=open_app_markup(),
        )
    except Exception:
        logger.exception("Не удалось отправить кнопку новому пользователю")


async def categories_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/categories — список категорий пользователя; редактирование — в приложении."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    cats = categories_service.list_categories(user["id"])
    lines = ["Твои категории:"] + [f"• {c['name']}" for c in cats]
    lines.append("\nДобавить, переименовать или перекрасить — в приложении: меню → «Категории».")
    await update.message.reply_text("\n".join(lines), reply_markup=open_app_markup())


async def web_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/web — личная ссылка для входа из обычного браузера (ноутбук, вне Telegram)."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    await update.message.reply_text(
        "Личная ссылка для браузера (никому её не пересылай: по ней открываются твои задачи):\n"
        f"{APP_URL}?key={user['access_key']}\n\n"
        "Как поставить иконку на телефон:\n"
        "iPhone — открой ссылку в Safari (⋯ → «Открыть в Safari»), затем «Поделиться» → «На экран Домой».\n"
        "Android — открой в Chrome, затем ⋮ → «Добавить на главный экран».",
        disable_web_page_preview=True,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    raw_text = update.message.text
    await update.message.chat.send_action("typing")

    try:
        tasks = task_service.create_tasks_from_text(raw_text, user["id"])
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
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    try:
        tasks = task_service.list_tasks(user["id"], status="To Do")
    except Exception:
        logger.exception("Ошибка при получении списка задач")
        await update.message.reply_text("Не получилось получить список задач.")
        return

    if not tasks:
        await update.message.reply_text("Активных задач нет 🎉")
        return

    lines = ["Активные задачи:"] + [_format_task_line(t) for t in tasks]
    await update.message.reply_text("\n".join(lines))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое сообщение → распознавание речи (Whisper) → тот же парсер задач."""
    user = _known_user(update)
    if not user:  # проверяем до распознавания, чтобы не тратить запросы к Groq
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    await update.message.chat.send_action("typing")

    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        recognized_text = transcribe_audio(audio_bytes)
    except Exception:
        logger.exception("Ошибка распознавания голосового сообщения")
        await update.message.reply_text("Не удалось распознать голосовое сообщение 😕")
        return

    if not recognized_text.strip():
        await update.message.reply_text("Не расслышал ничего внятного, попробуй ещё раз.")
        return

    try:
        tasks = task_service.create_tasks_from_text(recognized_text, user["id"])
    except Exception:
        logger.exception("Ошибка при разборе текста из голосового сообщения")
        await update.message.reply_text(f"🎙 Расслышал: «{recognized_text}», но не смог разобрать на задачи.")
        return

    if not tasks:
        await update.message.reply_text(f"🎙 Расслышал: «{recognized_text}», но задач в этом не нашёл.")
        return

    lines = [f"🎙 Расслышал: «{recognized_text}»", "Добавил:"] + [_format_task_line(t) for t in tasks]
    await update.message.reply_text("\n".join(lines))


def register_handlers(app: Application) -> None:
    """Одинаковый набор обработчиков для webhook (main.py) и локального polling (bot.py)."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_tasks_handler))
    app.add_handler(CommandHandler("web", web_link))
    app.add_handler(CommandHandler("categories", categories_handler))
    app.add_handler(CallbackQueryHandler(access_decision, pattern=r"^(approve|deny):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
