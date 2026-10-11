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
import secrets
import logging
import os
import time
from datetime import date, datetime

from types import SimpleNamespace

from telegram import (BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from groq_client import transcribe_audio
from models import TaskCreate
import categories_service
import comments_service
import digest_service
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
    BotCommand("help", "Все команды с кнопками"),
    BotCommand("today", "План на сегодня: просроченное, сегодня, ближайшие 24 часа"),
    BotCommand("list", "Активные задачи"),
    BotCommand("task", "Записать задачу, даже если в тексте есть сумма"),
    BotCommand("spent", "Записать расход: /spent кофе 3,5 евро"),
    BotCommand("income", "Записать доход: /income зарплата 1800 евро"),
    BotCommand("month", "Итоги месяца по деньгам"),
    BotCommand("last", "Последние операции"),
    BotCommand("currency", "Валюта по умолчанию: /currency EUR или KZT"),
    BotCommand("digest", "Утренняя и вечерняя сводка: /digest on или off"),
    BotCommand("split", "Найти задачи в тексте и выбрать, какие добавить"),
    BotCommand("save", "Цели: /save 100 отпуск — отложить, /save — прогресс"),
    BotCommand("remind", "Напоминать до задачи: /remind 15, 30, 60 или off"),
    BotCommand("backup", "Прислать резервную копию моих данных"),
    BotCommand("applepay", "Как подключить траты из Apple Pay"),
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


# Постоянная панель под полем ввода: частые действия одним нажатием. Текст кнопки — это просто сообщение,
# handle_text узнаёт его раньше разбора задач и денег (MENU_ACTIONS), поэтому задачей оно не станет.
BTN_TODAY, BTN_TASKS, BTN_MONTH, BTN_LAST, BTN_HELP = "📋 Сегодня", "📝 Все задачи", "💰 Месяц", "🧾 Последние траты", "❓ Помощь"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_TODAY), KeyboardButton(BTN_TASKS)],
         [KeyboardButton(BTN_MONTH), KeyboardButton(BTN_LAST)],
         [KeyboardButton(BTN_HELP)]],
        resize_keyboard=True, is_persistent=True, input_field_placeholder="Задача или трата…",
    )


HELP_TEXT = (
    "❓ Что я умею\n\n"
    "📝 Задачи — просто напишите или наговорите: «завтра в 18:00 сдать отчёт #работа»\n"
    "/today — план на сегодня: просроченное, сегодня, ближайшие 24 часа\n"
    "/list — все активные задачи\n"
    "/task текст — записать задачей, даже если в тексте есть сумма\n"
    "/split текст — найти задачи в тексте (заметке, письме) и выбрать, какие добавить\n"
    "/categories — категории задач\n\n"
    "💰 Деньги — просто напишите: «кофе 3,5 евро», «получил зарплату 1800 евро»\n"
    "/spent текст, /income текст — точно расход или доход\n"
    "/month — итоги месяца\n"
    "/last — последние траты с кнопкой «Удалить»\n"
    "/currency EUR или KZT — основная валюта\n"
    "/save — цели накопления; /save 100 отпуск — отложить, /save -50 отпуск — снять\n"
    "/applepay — как подключить траты из Apple Pay\n\n"
    "⚙️ Настройки\n"
    "/digest on или off — сводки утром в 08:00 и вечером в 20:00 (в воскресенье — итоги недели)\n"
    "/remind 15, 30, 60 или off — за сколько минут напоминать о задаче со временем\n"
    "/backup — резервная копия ваших данных файлом (она и так приходит по воскресеньям)\n"
    "/web — личная ссылка на приложение для браузера\n\n"
    "Кнопки ниже выполняют команду сразу."
)


def help_markup() -> InlineKeyboardMarkup:
    b = lambda text, cmd: InlineKeyboardButton(text, callback_data=f"h:{cmd}")
    return InlineKeyboardMarkup([
        [b("📋 Сегодня", "today"), b("📝 Все задачи", "list")],
        [b("💰 Месяц", "month"), b("🧾 Последние", "last")],
        [b("💱 Валюта", "currency"), b("🔔 Сводки", "digest")],
        [b("⏰ Напоминания", "remind"), b("🗄 Копия данных", "backup")],
        [b("🎯 Цели", "save")],
        [b("🏷 Категории", "categories"), b("💳 Apple Pay", "applepay")],
        [b("🔗 Ссылка /web", "web"), InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=APP_URL))],
    ])


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
    payload = (context.args[0] if getattr(context, "args", None) else "") or ""
    if payload.startswith("g_"):  # ссылка-приглашение в группу (groups_service)
        token = payload[2:]
        if not user:
            await update.message.reply_text("Чтобы вступить в группу, сначала нужен доступ к JulYera. "
                                            "Отправил запрос владельцу — после одобрения откройте ссылку ещё раз.")
        else:
            import groups_service
            group, text = groups_service.preview(token, user["id"])
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Вступить", callback_data=f"gj:y:{token}"),
                                            InlineKeyboardButton("✖️ Нет", callback_data=f"gj:n:{token}")]]) if group else None
            await update.message.reply_text(text, reply_markup=markup)
            return
    if user:
        await update.message.reply_text(
            f"Привет, {user['name']}! Напиши мне задачу текстом или голосом — я разберу её и сохраню.\n"
            "Траты и доходы тоже сюда: «кофе 3,5 евро», «получил зарплату 1800 евро».\n\n"
            "Частые действия — на кнопках внизу, все команды — «❓ Помощь».",
            reply_markup=main_keyboard(),
        )
        await update.message.reply_text("Приложение — кнопкой ниже или «Меню» слева от поля ввода.",
                                        reply_markup=open_app_markup())
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
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text="Частые действия — на кнопках внизу, все команды — «❓ Помощь».",
            reply_markup=main_keyboard(),
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
    await update.message.reply_text("\n".join(lines), reply_markup=_comment_buttons(tasks))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    raw_text = update.message.text
    reply_ids = _task_ids_from(getattr(update.message, "reply_to_message", None))
    if reply_ids:  # ответ на напоминание или «Добавил: …» — это комментарий к задаче, а не новая задача
        await _comment_or_ask(update, user, reply_ids, text=raw_text)
        return
    action = MENU_ACTIONS.get((raw_text or "").strip())
    if action:  # нажата кнопка панели — это команда, а не задача
        await globals()[action](update, context)
        return
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
    await update.message.reply_text("\n".join(lines), reply_markup=_comment_buttons(tasks))


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


def _tx_markup(tx_id: str, with_task: bool = True) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("Отменить", callback_data=f"m:u:{tx_id}"),
           InlineKeyboardButton("Категория", callback_data=f"m:c:{tx_id}")]
    if with_task:  # у покупок Apple Pay «Это задача» не нужна
        row.append(InlineKeyboardButton("Это задача", callback_data=f"m:t:{tx_id}"))
    return InlineKeyboardMarkup([row])


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


APPLEPAY_RU = (
    "💳 Траты из Apple Pay — автоматически (iPhone, iOS 17 и новее)\n\n"
    "1. «Команды» → вкладка «Автоматизация» → «+» → «Wallet» (в старых версиях — «Транзакция»).\n"
    "2. Выберите свои карты, категории и магазины оставьте «Любые». Внизу «Выполнять сразу», "
    "«Уведомлять» выключите → «Далее».\n"
    "3. «Новая пустая автоматизация» → «Добавить действие» → «Получить содержимое URL».\n"
    "4. URL: {url}\n"
    "5. Стрелка «›» у действия:\n"
    "   • Метод: POST\n"
    "   • Заголовки → «Добавить новый»: ключ X-Access-Key, значение — ваш ключ (последнее сообщение)\n"
    "   • Текст запроса: JSON → три поля «Текст», ключи ровно такие:\n"
    "     amount, merchant, card\n"
    "   • Значение каждого поля: нажмите на него → «Выбрать переменную» → наверху нажмите синюю плашку "
    "«Транзакция» (под «Когда … поднесена»; в старых iOS — «Ввод команды») → нажмите вставленную плашку и выберите:\n"
    "     amount → «Сумма», merchant → «Продавец», card → «Карта или пропуск»\n"
    "   Должно получиться три цветные плашки. Кнопка ▶ без оплаты ответит «Не нашёл сумму» — это нормально.\n"
    "6. «Готово». Заплатите телефоном — я пришлю «💳 Apple Pay: …». "
    "Если iPhone спросит про доступ к julyera-agent.onrender.com — «Разрешать всегда».\n"
    "Категория не та — нажмите «Категория» под сообщением, для этого магазина я запомню."
)

APPLEPAY_EN = (
    "💳 Apple Pay expenses — automatic (iPhone, iOS 17+)\n\n"
    "1. Shortcuts → Automation → + → Wallet (older iOS: Transaction).\n"
    "2. Select your cards, keep Categories and Merchants as Any. Choose Run Immediately, "
    "turn off Notify When Run → Next.\n"
    "3. New Blank Automation → Add Action → Get Contents of URL.\n"
    "4. URL: {url}\n"
    "5. Tap › (Show More) on the action:\n"
    "   • Method: POST\n"
    "   • Headers → Add new header: key X-Access-Key, value — your key (the last message)\n"
    "   • Request Body: JSON → three Text fields, keys exactly:\n"
    "     amount, merchant, card\n"
    "   • Value of each field: tap it → Select Variable → tap the blue Transaction token at the top "
    "(under «When Any Card is tapped»; older iOS: Shortcut Input) → tap the inserted token and pick:\n"
    "     amount → Amount, merchant → Merchant, card → Card or Pass\n"
    "   You should see three coloured tokens. Tapping ▶ without a payment answers «Не нашёл сумму» — that's fine.\n"
    "6. Done. Pay with your phone — I'll send «💳 Apple Pay: …». "
    "If iPhone asks to connect to julyera-agent.onrender.com — Always Allow.\n"
    "Wrong category? Tap «Категория» under my message — I'll remember it for that shop."
)


async def applepay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/applepay — инструкция для автоматизации Wallet («Транзакция») в «Командах» на русском и английском,
    затем личный ключ отдельным сообщением (удобно скопировать)."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    url = f"{APP_URL}money/applepay"
    await update.message.reply_text(APPLEPAY_RU.format(url=url), disable_web_page_preview=True)
    await update.message.reply_text(APPLEPAY_EN.format(url=url), disable_web_page_preview=True)
    await update.message.reply_text("🔑 Ваш ключ / your key — никому не пересылайте / don't share it:")
    await update.message.reply_text(user["access_key"])


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
    main = data.get("main")
    if main and [c["currency"] for c in data["currencies"]] != [main["currency"]]:
        sign = "" if main["balance"].startswith("-") else "+"
        lines.append(f"Всего в {main['currency']}: расходы ≈ {main['expense']} · доходы ≈ {main['income']} · "
                     f"баланс ≈ {sign}{main['balance']}")
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
        tx = money_service.update_transaction(tx["id"], uid, {"category_id": cat["id"]})  # Apple Pay — запомнит магазин
        applepay = tx.get("source") == "applepay"
        await query.answer(f"Категория: {cat['name']}" + (" — запомнил для этого магазина" if applepay else ""))
        await query.edit_message_text(("💳 Apple Pay: " if applepay else "Добавлено: ") + _money_line(tx, _cat_names(uid)),
                                      reply_markup=_tx_markup(tx["id"], with_task=not applepay))
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


# ---------------------------------------------------------------- сводки и напоминания

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — просроченные, на сегодня и в ближайшие 24 часа; только свои задачи."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    await update.message.reply_text(digest_service.today_text(user["id"]), reply_markup=open_app_markup())


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest on|off — утренняя и вечерняя сводка."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    arg = _command_text(update.message.text).lower()
    if arg not in ("on", "off"):
        state = "включены" if user.get("digest_enabled", True) is not False else "выключены"
        await update.message.reply_text(f"Сводки сейчас {state}. Включить: /digest on, выключить: /digest off.")
        return
    digest_service.set_enabled(user["id"], arg == "on")
    await update.message.reply_text("Сводки включены: утром в 08:00 и вечером в 20:00." if arg == "on"
                                    else "Сводки выключены. Напоминания о дедлайнах остаются.")


_SAVE_RE = re.compile(r"^\s*(-?\s*\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(.*)$", re.S)


def _goal_line(g: dict) -> str:
    bar = "▓" * (g["percent"] // 10) + "░" * (10 - g["percent"] // 10)
    tail = " ✅" if g["achieved"] else (f" · до {g['due_date'][8:10]}.{g['due_date'][5:7]}" if g.get("due_date") else "")
    return f"🎯 {g['name']}: {g['saved']} из {g['target']} {g['currency']}\n{bar} {g['percent']}%{tail}"


async def _apply_goal_move(reply, user: dict, goal: dict, amount) -> None:
    import goals_service
    try:
        updated = goals_service.add_move(goal["id"], user["id"], amount, notify=False)
    except money_service.MoneyError as exc:
        await reply(exc.message)
        return
    verb = "Отложил" if amount > 0 else "Снял"
    text = f"{verb} {money_service.fmt(abs(amount))} {goal['currency']}.\n" + _goal_line(updated)
    if updated.get("first_achieved"):
        text += f"\n\n🎉 Цель «{goal['name']}» достигнута!"
    await reply(text)


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/save — цели с прогрессом; /save 100 отпуск — отложить; /save -50 отпуск — снять (в валюте цели)."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    import goals_service
    goals = goals_service.list_goals(user["id"])["goals"]
    if not goals:
        await update.message.reply_text("Целей пока нет. Создайте цель в приложении: Кошелёк → Цели → «+».",
                                        reply_markup=open_app_markup())
        return
    arg = _command_text(update.message.text)
    found = _SAVE_RE.match(arg or "")
    if not found:
        await update.message.reply_text("🎯 Ваши цели:\n\n" + "\n\n".join(_goal_line(g) for g in goals) +
                                        "\n\nОтложить: /save 100 отпуск. Снять: /save -50 отпуск.")
        return
    raw, rest = found.group(1).replace(" ", "").replace("\u00a0", ""), found.group(2).strip()
    negative = raw.startswith("-")
    try:
        amount = money_service.parse_amount(raw.lstrip("-"))
    except money_service.MoneyError as exc:
        await update.message.reply_text(exc.message)
        return
    amount = -amount if negative else amount
    said_cur = None
    for cur, markers in money_service.CURRENCY_MARKERS.items():
        for m in markers:
            if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", rest.lower()):
                said_cur = cur
                rest = re.sub(rf"(?<!\w){re.escape(m)}(?!\w)", "", rest, flags=re.I).strip()
    matches = goals_service.find_goal(user["id"], rest)
    active = [g for g in matches if not g["achieved"]] or matches
    if said_cur:
        active = [g for g in active if g["currency"] == said_cur]
        if not active:
            await update.message.reply_text(f"Нет цели в {said_cur} с таким названием. Суммы в цель — в её валюте.")
            return
    if len(active) == 1:
        await _apply_goal_move(update.message.reply_text, user, active[0], amount)
        return
    if not active:
        await update.message.reply_text("Не нашёл такую цель. Ваши цели: " + ", ".join(g["name"] for g in goals))
        return
    rows = [[InlineKeyboardButton(_short(g["name"], 30), callback_data=f"gs:{g['id']}:{money_service.fmt(amount)}")]
            for g in active[:8]]
    await update.message.reply_text("В какую цель?", reply_markup=InlineKeyboardMarkup(rows))


async def group_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Вступить / Нет» по ссылке на группу (gj:y|n:<токен>): вступает тот, кто нажал."""
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    await query.answer()
    if not user:
        return
    import groups_service
    _, choice, token = query.data.split(":", 2)
    await query.edit_message_text(groups_service.join(token, user["id"]) if choice == "y" else "Хорошо, не вступаю.")


async def goal_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    _, goal_id, raw = (query.data.split(":") + ["", ""])[:3]
    await query.answer()
    if not user:
        return
    import goals_service
    from decimal import Decimal
    try:
        goal = goals_service.get_goal(goal_id, user["id"])
    except money_service.MoneyError as exc:
        await query.edit_message_text(exc.message)
        return

    async def reply(text):
        await query.edit_message_text(text)
    await _apply_goal_move(reply, user, goal, Decimal(raw))


_pending_splits: dict[str, dict] = {}   # /split: токен → найденные задачи и отметки (15 минут в памяти)
SPLIT_TTL = 15 * 60
SPLIT_MAX = 10


def _split_markup(token: str, pending: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(pending["tasks"]):
        mark = "☑️" if i in pending["picked"] else "⬜️"
        rows.append([InlineKeyboardButton(f"{mark} {_short(t['task'], 40)}", callback_data=f"sp:{token}:{i}")])
    n = len(pending["picked"])
    rows.append([InlineKeyboardButton(f"Добавить ({n})" if n else "Ничего не выбрано", callback_data=f"sp:{token}:ok"),
                 InlineKeyboardButton("Отмена", callback_data=f"sp:{token}:x")])
    return InlineKeyboardMarkup(rows)


def _split_line(t: dict) -> str:
    when = ""
    if t.get("deadline"):
        d = datetime.fromisoformat(t["deadline"]).replace(tzinfo=None)
        when = f" — {d.strftime('%d.%m')}" + ("" if d.strftime("%H:%M") == "23:59" else d.strftime(" %H:%M"))
    return f"• {t['task']}{when} · {t['category']}"


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/split текст — задачи из текста с галочками; добавляются только отмеченные."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    text = _command_text(update.message.text)
    if not text:
        await update.message.reply_text("Пришлите текст после команды: /split созвон — прислать презентацию до пятницы, "
                                        "забронировать зал в среду. Я найду задачи, а вы выберете, какие добавить.")
        return
    try:
        found = task_service.preview_tasks_from_text(text, user["id"])[:SPLIT_MAX]
    except Exception as exc:
        logger.warning("/split: разбор не удался: %s", type(exc).__name__)
        await update.message.reply_text("Не получилось разобрать текст, попробуйте ещё раз.")
        return
    if not found:
        await update.message.reply_text("Задач в этом тексте не нашёл.")
        return
    now = time.time()
    for key in [k for k, v in _pending_splits.items() if now - v["at"] > SPLIT_TTL]:
        _pending_splits.pop(key, None)
    token = secrets.token_hex(4)
    _pending_splits[token] = {"user_id": user["id"], "tasks": found, "picked": set(range(len(found))), "at": now}
    lines = [f"Нашёл задач: {len(found)}. Снимите галочки с лишних и нажмите «Добавить»:"] + [_split_line(t) for t in found]
    await update.message.reply_text("\n".join(lines), reply_markup=_split_markup(token, _pending_splits[token]))


async def split_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    _, token, choice = (query.data.split(":") + ["", ""])[:3]
    pending = _pending_splits.get(token)
    if not user or not pending or pending["user_id"] != user["id"]:
        await query.answer("Выбор устарел — пришлите /split ещё раз")
        return
    if choice == "x":
        _pending_splits.pop(token, None)
        await query.answer("Отменено")
        await query.edit_message_text("Ничего не добавил.")
        return
    if choice == "ok":
        if not pending["picked"]:
            await query.answer("Отметьте хотя бы одну задачу")
            return
        _pending_splits.pop(token, None)
        await query.answer()
        created = []
        for i in sorted(pending["picked"]):
            try:
                created.append(task_service.create_task(TaskCreate(**pending["tasks"][i]), user["id"]))
            except Exception as exc:
                logger.warning("/split: задача не создана: %s", type(exc).__name__)
        lines = ["Добавил:"] + [_format_task_line(t) for t in created]
        await query.edit_message_text("\n".join(lines) if created else "Не получилось добавить задачи.")
        return
    try:
        i = int(choice)
        pending["tasks"][i]
    except (ValueError, IndexError):
        await query.answer()
        return
    pending["picked"].symmetric_difference_update({i})
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=_split_markup(token, pending))


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remind 15|30|60|off — за сколько минут до задачи со временем присылать напоминание."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    arg = _command_text(update.message.text).lower().replace("мин", "").strip()
    minutes = 0 if arg in ("off", "выкл", "0") else int(arg) if arg.isdigit() else None
    if minutes not in digest_service.REMIND_CHOICES:
        current = digest_service.remind_before(user)
        state = f"за {current} мин" if current else "выключено"
        await update.message.reply_text(f"Напоминание до задачи со временем: {state}.\n"
                                        "Поменять: /remind 15, /remind 30, /remind 60 или /remind off.")
        return
    digest_service.set_remind_before(user["id"], minutes)
    await update.message.reply_text(f"Буду напоминать за {minutes} мин до задачи со временем." if minutes
                                    else "Напоминания перед задачами выключены. Сводки и напоминания в 13:00 и 17:00 остаются.")


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — файл с данными пользователя прямо сейчас (только его данные, в его чат)."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    import backup_service
    try:
        name, content, count = backup_service.build(user["id"])
        await context.bot.send_document(chat_id=update.effective_chat.id, document=content, filename=name,
                                        caption=backup_service.caption(count))
    except Exception as exc:
        logger.warning("Резервная копия не отправлена: %s", type(exc).__name__)
        await update.message.reply_text("Не получилось собрать копию, попробуйте чуть позже.")


def _without_rows(message, needle: str):
    """Клавиатура сообщения без строк, где есть кнопка с needle в callback_data."""
    markup = getattr(message, "reply_markup", None)
    rows = [list(row) for row in (getattr(markup, "inline_keyboard", None) or [])
            if not any(needle in (getattr(b, "callback_data", "") or "") for b in row)]
    return InlineKeyboardMarkup(rows) if rows else None


async def _append_and_trim(query, line: str, needle: str) -> None:
    text = ((query.message.text if query.message else "") or "").rstrip()
    try:
        await query.edit_message_text(f"{text}\n{line}" if text else line,
                                      reply_markup=_without_rows(query.message, needle))
    except Exception as exc:  # например, сообщение уже изменено — нажатие всё равно обработано
        logger.warning("Сообщение напоминания не обновлено: %s", type(exc).__name__)


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Готово» (done:<id>) и «Отложить на час» (snooze:<id>) под напоминанием.
    Пользователь — по from.id; чужая или удалённая задача — «Задача не найдена», без изменений.
    Повторное нажатие безопасно. answerCallbackQuery вызывается всегда."""
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    if not user:
        await query.answer()
        return
    action, _, task_id = (query.data or "").partition(":")
    task = task_service.get_task(task_id, user["id"]) if task_id else None
    if not task:
        await query.answer("Задача не найдена")
        return
    if action == "done":
        status, _ = digest_service.complete(task_id, user["id"])
        await query.answer("Уже выполнено" if status == "already" else "Готово ✅")
        await _append_and_trim(query, f"✅ Выполнено: {task['task']}", task_id)
        return
    if action in ("mv1", "mv2"):
        status, moved = digest_service.reschedule(task_id, user["id"], 1 if action == "mv1" else 2)
        if status == "already":
            await query.answer("Задача уже выполнена")
            await _append_and_trim(query, f"✅ Выполнено: {task['task']}", task_id)
            return
        day = digest_service._deadline(moved)
        label = f"{day.day} {digest_service.MONTHS_GEN[day.month - 1]}" if day else "завтра"
        await query.answer(f"Перенёс на {label}")
        await _append_and_trim(query, f"➡️ Перенесено на {label}: {task['task']}", task_id)
        return
    status, until = digest_service.snooze(task_id, user["id"])
    if status == "already":
        await query.answer("Задача уже выполнена")
        await _append_and_trim(query, f"✅ Выполнено: {task['task']}", task_id)
        return
    await query.answer(f"Напомню в {until:%H:%M}")
    await _append_and_trim(query, f"⏰ Отложено до {until:%H:%M}: {task['task']}", task_id)


async def digest_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Перенести невыполненное на завтра» под вечерней сводкой (dmove:<YYYY-MM-DD>)."""
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    if not user:
        await query.answer()
        return
    try:
        day = date.fromisoformat((query.data or "").partition(":")[2])
    except ValueError:
        await query.answer()
        return
    status, moved = digest_service.move_unfinished(user["id"], day)
    if status == "stale":
        await query.answer("Эта сводка уже устарела", show_alert=True)
        return
    await query.answer(f"Перенесено: {moved}" if moved else "Уже перенесено")
    await _append_and_trim(query, f"➡️ Перенесено на завтра: {moved}" if moved else "➡️ Переносить уже нечего", "dmove:")


# ---------------------------------------------------------------- комментарии к задачам

# Задача из сообщения бота, на которое ответил человек: кнопки done:/snooze:/cmt: с id задачи
# и сообщения-приглашения «Ответьте на это сообщение…» (ForceReply, id помним в памяти процесса).
_TASK_ID_RE = re.compile(r"^(?:done|snooze|mv1|mv2|cmt):([0-9a-fA-F-]{36})$")
_prompt_targets: dict[tuple[int, int], str] = {}
_pending_comments: dict[str, dict] = {}   # «К какой задаче?»: токен → что прикрепить и среди чего выбирать
PENDING_TTL = 15 * 60


def _comment_buttons(tasks: list[dict]):
    rows = [[InlineKeyboardButton(f"💬 {_short(t['task'])}" if len(tasks) > 1 else "💬 Комментарий",
                                  callback_data=f"cmt:{t['id']}")] for t in tasks[:5]]
    return InlineKeyboardMarkup(rows) if rows else None


def _short(text: str, n: int = 28) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _task_ids_from(message) -> list[str]:
    if message is None:
        return []
    chat = getattr(message, "chat", None)
    key = (getattr(chat, "id", None), getattr(message, "message_id", None))
    if key in _prompt_targets:
        return [_prompt_targets[key]]
    ids = []
    markup = getattr(message, "reply_markup", None)
    for row in (getattr(markup, "inline_keyboard", None) or []):
        for button in row:
            found = _TASK_ID_RE.match(getattr(button, "callback_data", "") or "")
            if found and found.group(1) not in ids:
                ids.append(found.group(1))
    return ids


def _candidate_tasks(user_id: str) -> list[dict]:
    """Кому прикрепить, если неясно: ближайшие незакрытые задачи (сначала с дедлайном), до 8."""
    tasks = [t for t in task_service.list_tasks(user_id) if t.get("status") != "Done"]
    tasks.sort(key=lambda t: (t.get("deadline") is None, t.get("deadline") or ""))
    return tasks[:8]


async def _attach(reply, user: dict, task_id: str, text=None, file=None) -> None:
    """Добавить комментарий и ответить. file — (байты, тип, имя)."""
    data, ftype, fname = file if file else (None, None, None)
    try:
        comment = comments_service.add_comment(task_id, user["id"], text, data, ftype, fname)
    except comments_service.CommentError as exc:
        await reply(exc.message)
        return
    except Exception as exc:
        logger.warning("Комментарий из бота не сохранён: %s", type(exc).__name__)
        await reply("Не получилось сохранить комментарий, попробуйте ещё раз.")
        return
    if comment is None:
        await reply("Задача не найдена — возможно, её уже удалили.")
        return
    task = task_service.get_task(task_id, user["id"]) or {"task": "?"}
    what = "📎 Добавил " + ("PDF" if ftype == "application/pdf" else "фото") if file else "💬 Добавил комментарий"
    await reply(f"{what} к задаче «{task['task']}»")


async def _comment_or_ask(update: Update, user: dict, task_ids: list[str], text=None, file=None) -> None:
    """Одна задача — прикрепить сразу; иначе спросить кнопками, к какой (выбор помним 15 минут)."""
    reply = update.message.reply_text
    if len(task_ids) == 1:
        await _attach(reply, user, task_ids[0], text, file)
        return
    tasks = [t for t in (task_service.get_task(i, user["id"]) for i in task_ids) if t] if task_ids else _candidate_tasks(user["id"])
    if not tasks:
        await reply("Нет активных задач, к которым можно прикрепить. Сначала создайте задачу.")
        return
    now = time.time()
    for token in [k for k, v in _pending_comments.items() if now - v["at"] > PENDING_TTL]:
        _pending_comments.pop(token, None)
    token = secrets.token_hex(4)
    _pending_comments[token] = {"user_id": user["id"], "ids": [t["id"] for t in tasks], "text": text, "file": file, "at": now}
    rows = [[InlineKeyboardButton(_short(t["task"], 40), callback_data=f"cm:{token}:{i}")] for i, t in enumerate(tasks)]
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"cm:{token}:x")])
    await reply("К какой задаче прикрепить?", reply_markup=InlineKeyboardMarkup(rows))


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото или PDF боту → комментарий к задаче (подпись — текст комментария)."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    msg = update.message
    if msg.photo:
        tg_file_id, ftype, fname = msg.photo[-1].file_id, "image/jpeg", "фото.jpg"
    else:
        doc = msg.document
        ftype = (doc.mime_type or "").lower()
        if ftype not in comments_service.FILE_TYPES:
            await msg.reply_text("Прикрепить можно фото или PDF.")
            return
        if (doc.file_size or 0) > comments_service.FILE_MAX:
            await msg.reply_text("Файл больше 10 МБ.")
            return
        tg_file_id, fname = doc.file_id, doc.file_name
    try:
        tg_file = await context.bot.get_file(tg_file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning("Файл из Telegram не скачан: %s", type(exc).__name__)
        await msg.reply_text("Не получилось скачать файл, попробуйте ещё раз.")
        return
    ids = _task_ids_from(getattr(msg, "reply_to_message", None))
    await _comment_or_ask(update, user, ids, text=msg.caption, file=(data, ftype, fname))


async def comment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cmt:<id> — «💬 Комментарий»: пригласить ответить; cm:<token>:<n> — выбор задачи из списка."""
    query = update.callback_query
    user = users_service.get_by_telegram_id(query.from_user.id)
    if not user:
        await query.answer()
        return
    data = query.data or ""
    if data.startswith("cmt:"):
        task = task_service.get_task(data[4:], user["id"])
        await query.answer()
        if not task:
            await query.message.reply_text("Задача не найдена.")
            return
        prompt = await query.message.reply_text(
            f"💬 Ответьте на это сообщение текстом, фото или PDF — добавлю к задаче «{task['task']}».",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Комментарий к задаче"))
        if prompt is not None:
            _prompt_targets[(prompt.chat.id, prompt.message_id)] = task["id"]
        return
    _, token, choice = (data.split(":") + ["", ""])[:3]
    pending = _pending_comments.get(token)
    if not pending or pending["user_id"] != user["id"]:
        await query.answer("Выбор устарел — пришлите ещё раз")
        return
    if choice == "x":
        _pending_comments.pop(token, None)
        await query.answer("Отменено")
        await query.edit_message_text("Не прикрепляю.")
        return
    try:
        task_id = pending["ids"][int(choice)]
    except (ValueError, IndexError):
        await query.answer()
        return
    _pending_comments.pop(token, None)
    await query.answer()

    async def reply(text):
        await query.edit_message_text(text)
    await _attach(reply, user, task_id, pending["text"], pending["file"])


# ---------------------------------------------------------------- помощь

MENU_ACTIONS = {BTN_TODAY: "today_command", BTN_TASKS: "list_tasks_handler", BTN_MONTH: "month_command",
                BTN_LAST: "last_command", BTN_HELP: "help_command"}
HELP_ACTIONS = {"today": "today_command", "list": "list_tasks_handler", "month": "month_command",
                "last": "last_command", "currency": "currency_command", "digest": "digest_command",
                "categories": "categories_handler", "applepay": "applepay_command", "web": "web_link",
                "remind": "remind_command", "backup": "backup_command", "save": "save_command"}


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help и «❓ Помощь»: все команды по группам и кнопки, которые выполняют команду сразу.
    Заодно (заново) показывает панель кнопок под полем ввода."""
    user = _known_user(update)
    if not user:
        await update.message.reply_text(NO_ACCESS_TEXT)
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=help_markup())
    await update.message.reply_text("Кнопки частых действий — внизу 👇", reply_markup=main_keyboard())


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка под /help: выполнить команду как будто её написали (без аргументов)."""
    query = update.callback_query
    cmd = (query.data or "").partition(":")[2]
    action = HELP_ACTIONS.get(cmd)
    await query.answer()
    if not action or not query.message:
        return
    message = SimpleNamespace(text=f"/{cmd}", reply_text=query.message.reply_text, chat=query.message.chat)
    fake = SimpleNamespace(effective_user=update.effective_user, effective_chat=update.effective_chat, message=message)
    await globals()[action](fake, context)


def register_handlers(app: Application) -> None:
    """Одинаковый набор обработчиков для webhook (main.py) и локального polling (bot.py)."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_tasks_handler))
    app.add_handler(CommandHandler("web", web_link))
    app.add_handler(CommandHandler("categories", categories_handler))
    app.add_handler(CommandHandler("spent", spent_command))
    app.add_handler(CommandHandler("income", income_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("last", last_command))
    app.add_handler(CommandHandler("currency", currency_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("applepay", applepay_command))
    app.add_handler(CommandHandler("digest", digest_command))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(CommandHandler("save", save_command))
    app.add_handler(CallbackQueryHandler(group_join_callback, pattern=r"^gj:[yn]:[0-9a-f]{1,32}$"))
    app.add_handler(CallbackQueryHandler(goal_save_callback, pattern=r"^gs:[0-9a-f-]{36}:-?\d+\.\d{2}$"))
    app.add_handler(CallbackQueryHandler(split_callback, pattern=r"^sp:[0-9a-f]{8}:(\d+|ok|x)$"))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(access_decision, pattern=r"^(approve|deny):\d+$"))
    app.add_handler(CallbackQueryHandler(money_callback, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^h:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(comment_callback, pattern=r"^(cmt:[0-9a-fA-F-]{36}|cm:[0-9a-f]{8}:(\d+|x))$"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^(done|snooze|mv1|mv2):[0-9a-fA-F-]{36}$"))
    app.add_handler(CallbackQueryHandler(digest_move_callback, pattern=r"^dmove:\d{4}-\d{2}-\d{2}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
