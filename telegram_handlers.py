"""
Хендлеры Telegram-бота: используются и для локального polling (bot.py),
и для продакшен-webhook (main.py). Работают с задачами напрямую через
task_service — без HTTP-круга внутри процесса.

Доступ: пользоваться ботом и Mini App может только тот, кого владелец (is_admin)
разрешил кнопкой в ответ на /start. Неизвестный Telegram-аккаунт ничего не создаёт.

Деньги или задача: /spent, /income — всегда деньги, /task — всегда задача. Без команды деньгами
считается текст с числом рядом с валютой («кофе 3 евро») или с первым словом «расход»/«доход»
(money_service.money_mode), остальное — задачи, как раньше. Ошибку исправляют кнопки под ответом.
В логи не пишем тексты сообщений, суммы и заметки.
"""
import re
import logging
import os
import time
from datetime import date, datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from groq_client import transcribe_audio
import categories_service
import money_service
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

# Меню команд бота (ставится при запуске сервиса, main.py → set_my_commands).
BOT_COMMANDS = [
    BotCommand("list", "Активные задачи"),
    BotCommand("task", "Записать задачу, даже если в тексте есть сумма"),
    BotCommand("spent", "Записать расход: /spent кофе 3,5 евро"),
    BotCommand("income", "Записать доход: /income зарплата 1800 евро"),
    BotCommand("month", "Итоги месяца по деньгам"),
    BotCommand("last", "Последние операции"),
    BotCommand("currency", "Валюта по умолчанию: /currency EUR или KZT"),
    BotCommand("categories", "Категории задач"),
    BotCommand("web", "Ссылка на приложение для браузера"),
    BotCommand("start", "Помощь"),
]

MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_NOMINATIVE = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
                     "Октябрь", "Ноябрь", "Декабрь"]
MONEY_BATCH_MAX = 8  # в «Отменить всё» помещается не больше 8 коротких id (callback_data ≤ 64 байт)
SHORT_ID = 6         # длина префикса id операции в «Отменить всё»


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
            "Траты и доходы тоже сюда: «кофе 3,5 евро», «получил зарплату 1800 евро».\n\n"
            "Команды:\n"
            "/list — активные задачи\n"
            "/spent, /income — записать расход или доход\n"
            "/task — записать задачу, даже если в тексте есть сумма\n"
            "/month — итоги месяца, /last — последние операции, /currency — валюта по умолчанию\n"
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
    money_service.ensure_defaults(user["id"])  # стартовый набор категорий расходов и доходов
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


async def _record_tasks(update: Update, user: dict, text: str) -> None:
    """Прежний путь: текст → задачи."""
    try:
        tasks = task_service.create_tasks_from_text(text, user["id"])
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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    raw_text = update.message.text
    await update.message.chat.send_action("typing")
    is_money, kind, text = money_service.money_mode(raw_text)
    if is_money:
        await _record_money(update, user, text, force_kind=kind, source="text")
    else:
        await _record_tasks(update, user, raw_text)


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

    is_money, kind, money_text = money_service.money_mode(recognized_text)
    if is_money:
        await _record_money(update, user, money_text, force_kind=kind, source="voice", heard=recognized_text)
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


# ---------------------------------------------------------------- деньги

def _short_day(day: str) -> str:
    d = date.fromisoformat(day[:10])
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


def _money_line(tx: dict, cat_names: dict) -> str:
    """«Кофе · 3.50 EUR · Еда · 8 окт» (доход помечен знаком +)."""
    cat = cat_names.get(tx["category_id"], "?")
    label = tx.get("note") or cat
    sign = "+" if tx["kind"] == "income" else ""
    return f"{label} · {sign}{tx['amount']} {tx['currency']} · {cat} · {_short_day(tx['spent_on'])}"


def _cat_names(user_id: str) -> dict:
    return {c["id"]: c["name"] for c in money_service.list_categories(user_id)}


def _tx_markup(tx_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Отменить", callback_data=f"m:u:{tx_id}"),
        InlineKeyboardButton("Категория", callback_data=f"m:c:{tx_id}"),
        InlineKeyboardButton("Это задача", callback_data=f"m:t:{tx_id}"),
    ]])


def _command_text(text: str) -> str:
    """Текст после команды: «/spent@bot кофе 3 евро» → «кофе 3 евро»."""
    return re.sub(r"^/\S+\s*", "", text or "", count=1).strip()


async def _record_money(update: Update, user: dict, text: str, force_kind=None, source="text", heard=None) -> None:
    prefix = f"🎙 Расслышал: «{heard}»\n" if heard else ""
    try:
        drafts = money_service.drafts_from_text(user["id"], text, force_kind=force_kind)
    except Exception as exc:
        logger.warning("Разбор денег в боте не удался: %s", type(exc).__name__)
        await update.message.reply_text(prefix + "Не получилось разобрать трату, запись не создана. "
                                        "Попробуйте ещё раз или добавьте её в приложении.")
        return
    if not drafts:
        await update.message.reply_text(prefix + "Не нашёл сумму, напишите её числом.")
        return
    skipped = max(0, len(drafts) - MONEY_BATCH_MAX)
    created = []
    for draft in drafts[:MONEY_BATCH_MAX]:
        try:
            created.append(money_service.create_transaction(user["id"], draft, source=source))
        except Exception as exc:
            logger.warning("Операция из бота не сохранена: %s", type(exc).__name__)
    if not created:
        await update.message.reply_text(prefix + "Не получилось сохранить запись, попробуйте ещё раз.")
        return
    names = _cat_names(user["id"])
    if len(created) == 1:
        await update.message.reply_text(prefix + "Добавлено: " + _money_line(created[0], names),
                                        reply_markup=_tx_markup(created[0]["id"]), do_quote=True)
        return
    lines = [prefix + "Добавлено:"] + ["• " + _money_line(tx, names) for tx in created]
    if skipped:
        lines.append(f"Записал первые {MONEY_BATCH_MAX}, остальные добавьте отдельным сообщением.")
    short = ",".join(tx["id"][:SHORT_ID] for tx in created)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Отменить всё", callback_data=f"m:a:{short}")]])
    await update.message.reply_text("\n".join(lines), reply_markup=markup, do_quote=True)


async def spent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _money_command(update, "expense")


async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _money_command(update, "income")


async def _money_command(update: Update, kind: str) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    text = _command_text(update.message.text)
    if not text:
        example = "/spent кофе 3,5 евро" if kind == "expense" else "/income зарплата 1800 евро"
        await update.message.reply_text(f"Напишите после команды, что и сколько: {example}")
        return
    await update.message.chat.send_action("typing")
    await _record_money(update, user, text, force_kind=kind, source="text")


async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/task текст — всегда задача, даже если в тексте есть сумма."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    text = _command_text(update.message.text)
    if not text:
        await update.message.reply_text("Напишите задачу после команды: /task оплатить аренду 650 евро до пятницы")
        return
    await update.message.chat.send_action("typing")
    await _record_tasks(update, user, text)


async def month_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/month — итоги текущего месяца по каждой валюте и пять самых больших категорий расходов."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    data = money_service.summary(user["id"])
    month = date.fromisoformat(data["month"] + "-01")
    title = f"{MONTHS_NOMINATIVE[month.month - 1]} {month.year}"
    if not data["currencies"]:
        await update.message.reply_text(f"{title}: операций пока нет. Напишите, например, «кофе 3 евро».")
        return
    lines = [title]
    for cur in data["currencies"]:
        balance = cur["balance"] if cur["balance"].startswith("-") else "+" + cur["balance"]
        lines.append(f"\n{cur['currency']}: расходы {cur['expense']} · доходы {cur['income']} · баланс {balance}")
        for cat in cur["by_category"][:5]:
            lines.append(f"  {cat['name']}: {cat['amount']} ({cat['share']}%)")
    await update.message.reply_text("\n".join(lines), reply_markup=open_app_markup())


def _last_view(user_id: str):
    txs = money_service.last_transactions(user_id, 5)
    if not txs:
        return "Операций пока нет.", None
    names = _cat_names(user_id)
    lines = ["Последние операции:"] + [f"{i}. {_money_line(tx, names)}" for i, tx in enumerate(txs, 1)]
    buttons = [[InlineKeyboardButton(f"Удалить {i}", callback_data=f"m:d:{tx['id']}")] for i, tx in enumerate(txs, 1)]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    text, markup = _last_view(user["id"])
    await update.message.reply_text(text, reply_markup=markup)


async def currency_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    arg = _command_text(update.message.text).upper()
    options = " или ".join(money_service.CURRENCIES)
    if not arg:
        current = money_service.default_currency(user["id"])
        await update.message.reply_text(f"Валюта по умолчанию: {current}. Сменить: /currency {options}")
        return
    if arg not in money_service.CURRENCIES:
        await update.message.reply_text(f"Такой валюты нет. Можно: {options}")
        return
    money_service.set_default_currency(user["id"], arg)
    await update.message.reply_text(f"Готово: если валюта не названа, записываю в {arg}.")


def _original_text(message) -> str | None:
    """Исходный текст траты для «Это задача»: сообщение, на которое ответил бот, или расслышанное голосовое."""
    if message is None:
        return None
    source = getattr(message, "reply_to_message", None)
    if source is not None and getattr(source, "text", None):
        return _command_text(source.text) if source.text.startswith("/") else source.text
    found = re.search(r"Расслышал: «(.*)»", message.text or "", re.S)
    return found.group(1) if found else None


async def money_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под операциями: m:u — отменить, m:a — отменить всё, m:c — выбрать категорию,
    m:s — поставить категорию, m:t — это задача, m:d — удалить из /last.
    Пользователь — по from.id нажавшего; чужая операция — отказ без изменений. Повтор безопасен."""
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    if not user:
        await query.answer()
        return
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    uid = user["id"]

    if action == "a":
        prefixes = [p for p in arg.split(",") if p]
        recent = money_service.last_transactions(uid, 50)
        ids = [tx["id"] for tx in recent if any(tx["id"].startswith(p) for p in prefixes)]
        for tx_id in ids:
            money_service.delete_transaction(tx_id, uid)
        await query.answer("Отменено" if ids else "Уже отменено")
        await query.edit_message_text("↩️ Отменено: " + (f"операций {len(ids)}" if ids else "записи уже удалены"))
        return

    if action == "d":
        deleted = money_service.delete_transaction(arg, uid)
        await query.answer("Удалено" if deleted else "Операция не найдена")
        text, markup = _last_view(uid)
        await query.edit_message_text(text, reply_markup=markup)
        return

    tx = money_service.get_transaction(arg, uid)
    if not tx:
        await query.answer("Операция не найдена")
        return

    if action == "u":
        money_service.delete_transaction(tx["id"], uid)
        await query.answer("Отменено")
        await query.edit_message_text("↩️ Отменено: " + _money_line(tx, _cat_names(uid)))
        return

    if action == "c":
        cats = money_service.list_categories(uid, tx["kind"])[:8]
        rows, row = [], []
        for cat in cats:
            mark = "✓ " if cat["id"] == tx["category_id"] else ""
            row.append(InlineKeyboardButton(mark + cat["name"], callback_data=f"m:s:{tx['id']}:{cat['id'][:8]}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "s":
        prefix = parts[3] if len(parts) > 3 else ""
        cat = next((c for c in money_service.list_categories(uid, tx["kind"]) if prefix and c["id"].startswith(prefix)), None)
        if not cat:
            await query.answer("Категория не найдена")
            return
        tx = money_service.update_transaction(tx["id"], uid, {"category_id": cat["id"]})
        await query.answer(f"Категория: {cat['name']}")
        await query.edit_message_text("Добавлено: " + _money_line(tx, _cat_names(uid)), reply_markup=_tx_markup(tx["id"]))
        return

    if action == "t":
        original = _original_text(query.message)
        if not original:
            await query.answer("Не нашёл исходный текст. Напишите задачу через /task", show_alert=True)
            return
        money_service.delete_transaction(tx["id"], uid)
        await query.answer("Делаю задачей")
        try:
            tasks = task_service.create_tasks_from_text(original, uid)
        except Exception:
            logger.exception("Ошибка при разборе текста в задачу (кнопка «Это задача»)")
            tasks = []
        if tasks:
            await query.edit_message_text("Операция удалена, добавил задачу:\n" + "\n".join(_format_task_line(t) for t in tasks))
        else:
            await query.edit_message_text("Операция удалена, но задачу из текста создать не получилось. Напишите её через /task.")
        return

    await query.answer()


def register_handlers(app: Application) -> None:
    """Одинаковый набор обработчиков для webhook (main.py) и локального polling (bot.py)."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_tasks_handler))
    app.add_handler(CommandHandler("web", web_link))
    app.add_handler(CommandHandler("categories", categories_handler))
    app.add_handler(CommandHandler("spent", spent_command))
    app.add_handler(CommandHandler("income", income_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("last", last_command))
    app.add_handler(CommandHandler("currency", currency_command))
    app.add_handler(CallbackQueryHandler(access_decision, pattern=r"^(approve|deny):\d+$"))
    app.add_handler(CallbackQueryHandler(money_callback, pattern=r"^m:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
