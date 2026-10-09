"""
Backend для персонального агента структурирования задач (несколько пользователей).

Вход:
    X-Telegram-Init-Data — подписанные данные Telegram Mini App (основной способ);
    X-Access-Key         — личный ключ из ссылки /web (запасной, для браузера вне Telegram).
    Нет заголовков или подпись неверна → 401; подпись верна, но человек не допущен → 403 not_approved.

Эндпоинты (все, кроме отмеченных, требуют входа и работают только с данными текущего пользователя):
    POST   /tasks                    — создать задачу (структурированно)
    POST   /tasks/parse              — разобрать сырой текст через Groq и создать задачи
    GET    /tasks                    — список задач (с фильтрами)
    GET    /tasks/{id}               — одна задача
    PATCH  /tasks/{id}               — обновить задачу
    DELETE /tasks/{id}               — удалить задачу
    POST   /tasks/{id}/subtasks      — добавить шаг; GET /subtasks, PATCH/DELETE /subtasks/{id}
    GET/POST /notes, PATCH/DELETE /notes/{id}
    GET    /google/status, /google/auth-url, /google/auth — подключение своего Google Calendar
    GET    /google/callback          — возврат из Google (публичный, защищён подписанным state)
    POST   /telegram/webhook/{token} — обновления от Telegram (публичный, защищён токеном)
    POST   /internal/reminders/run   — напоминания (публичный, защищён X-Reminder-Secret)
    GET    /health                   — публичный

Локальный запуск:
    uvicorn main:app --reload

В продакшене (Render и т.п.) задай WEBHOOK_BASE_URL — при старте
приложение само зарегистрирует webhook в Telegram и кнопку меню Mini App.
Локально WEBHOOK_BASE_URL не задавай — вместо этого используй bot.py (polling).
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import task_service
import backup_service
import calendar_sync
import categories_service
import comments_service
import digest_service
import money_service
import notes_service
import subtasks_service
import users_service
import google_calendar_service as gcal
from database import supabase
from categories_service import CategoryError
from money_service import MoneyError
from telegram_auth import InitDataError, verify_init_data
from models import (
    CalendarChoice,
    CategoryCreate,
    CategoryOrder,
    CategoryUpdate,
    CommentCreate,
    ApplePayIn,
    MeUpdate,
    MoneyCategoryCreate,
    MoneyCategoryOrder,
    MoneyCategoryUpdate,
    MoneyParseRequest,
    RecurringCreate,
    RecurringUpdate,
    TransactionCreate,
    TransactionUpdate,
    ParseRequest,
    Priority,
    Status,
    Task,
    TaskCreate,
    TaskUpdate,
    Note,
    NoteCreate,
    NoteUpdate,
    Subtask,
    SubtaskCreate,
    SubtaskUpdate,
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")  # например https://task-agent.onrender.com
REMINDERS_SECRET = os.environ.get("REMINDERS_SECRET")  # секрет для /internal/reminders/run

telegram_app = None
if TELEGRAM_BOT_TOKEN:
    from telegram import MenuButtonWebApp, Update as TgUpdate, WebAppInfo
    from telegram.ext import Application as TgApplication

    from telegram_handlers import APP_URL, register_handlers

    telegram_app = TgApplication.builder().token(TELEGRAM_BOT_TOKEN).build()
    register_handlers(telegram_app)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app:
        await telegram_app.initialize()
        if WEBHOOK_BASE_URL:
            webhook_url = f"{WEBHOOK_BASE_URL}/telegram/webhook/{TELEGRAM_BOT_TOKEN}"
            # callback_query нужен для кнопок «Разрешить» / «Отклонить»
            await telegram_app.bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
            try:
                # Кнопка меню в чате с ботом открывает Mini App. Если не получилось —
                # настроить вручную в BotFather: Bot Settings → Menu Button.
                await telegram_app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=APP_URL))
                )
            except Exception:
                logger.exception("Не удалось установить кнопку меню Mini App")
            try:
                from telegram_handlers import BOT_COMMANDS
                await telegram_app.bot.set_my_commands(BOT_COMMANDS)
            except Exception:
                logger.exception("Не удалось установить список команд бота")
            try:
                import release_notes  # после деплоя — один раз сообщить всем пользователям, что изменилось
                from telegram_handlers import main_keyboard
                await release_notes.announce(telegram_app.bot, supabase, users_service.list_users(), main_keyboard())
            except Exception:
                logger.exception("Заметка о релизе не отправлена")
    yield
    if telegram_app:
        await telegram_app.shutdown()


app = FastAPI(title="Task Agent API", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def html_no_cache(request: Request, call_next):
    """Страница приложения: браузер каждый раз сверяется с сервером (по ETag — без повторной загрузки, если не
    менялась). Иначе после релиза телефон и Telegram показывают старую версию из кэша."""
    response = await call_next(request)
    if request.method == "GET" and response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(CategoryError)
async def category_error_handler(request: Request, exc: CategoryError):
    # 404 — чужая или несуществующая, 409 — дубль / последняя / есть задачи, 422 — неверное название или цвет
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(comments_service.CommentError)
async def comment_error_handler(request: Request, exc: comments_service.CommentError):
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(MoneyError)
async def money_error_handler(request: Request, exc: MoneyError):
    # Тот же формат, что у категорий: {"detail": {"code", "message"}}; message — понятный текст по-русски
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


def _deny(request: Request, status: int, reason: str, detail) -> HTTPException:
    # В лог Render — только причина одним словом, метод и путь; значения initData и ключей не пишем.
    logger.warning("Вход отклонён (%s): %s %s → %s", reason, request.method, request.url.path, status)
    return HTTPException(status_code=status, detail=detail)


def current_user(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(None),
    x_access_key: Optional[str] = Header(None),
) -> dict:
    """Текущий пользователь из проверенного входа. user_id дальше берётся только отсюда."""
    if x_telegram_init_data:
        try:
            tg_user = verify_init_data(x_telegram_init_data, TELEGRAM_BOT_TOKEN or "")
        except InitDataError as exc:
            raise _deny(request, 401, exc.reason, {"code": "init_data_" + ("expired" if exc.reason == "устарел" else "invalid"),
                                                   "message": "Неверные или устаревшие данные входа Telegram"})
        user = users_service.get_by_telegram_id(tg_user["id"])
        if not user:
            raise _deny(request, 403, "не одобрен",
                        {"code": "not_approved", "message": "Доступ ещё не одобрен владельцем"})
        return user
    if x_access_key:
        user = users_service.get_by_access_key(x_access_key)
        if not user:
            raise _deny(request, 401, "ключ неверный", {"code": "bad_key", "message": "Неверный ключ доступа"})
        return user
    raise _deny(request, 401, "нет заголовка", {"code": "no_auth", "message": "Нужен вход через Telegram"})


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(payload: TaskCreate, user: dict = Depends(current_user)):
    return task_service.create_task(payload, user["id"])


@app.post("/tasks/preview")
def preview_tasks(payload: ParseRequest, user: dict = Depends(current_user)):
    """Задачи из текста без сохранения (кнопка «Задачи из заметки»): клиент показывает их с галочками
    и создаёт отмеченные обычным POST /tasks."""
    try:
        return task_service.preview_tasks_from_text(payload.text, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка разбора LLM: {exc}")


@app.post("/tasks/{task_id}/breakdown", status_code=201)
def breakdown_task(task_id: UUID, user: dict = Depends(current_user)):
    """«Разбей на шаги»: новые подзадачи от ИИ (уже сохранены). Чужая задача — 404."""
    try:
        added = task_service.breakdown(str(task_id), user["id"])
    except Exception as exc:
        logger.warning("Шаги не предложены: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="ИИ не ответил, попробуйте ещё раз")
    if added is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return {"added": added}


@app.post("/tasks/parse", response_model=list[Task], status_code=201)
def parse_and_create_tasks(payload: ParseRequest, user: dict = Depends(current_user)):
    try:
        created = task_service.create_tasks_from_text(payload.text, user["id"], source_hint=payload.source,
                                                      default_date=payload.default_date)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка разбора LLM: {exc}")

    if not created:
        raise HTTPException(
            status_code=422,
            detail="Не удалось извлечь ни одной подходящей задачи из текста",
        )
    return created


@app.get("/tasks", response_model=list[Task])
def list_tasks(
    category: Optional[str] = Query(None, description="название категории пользователя"),
    status: Optional[Status] = Query(None),
    priority: Optional[Priority] = Query(None),
    deadline_before: Optional[datetime] = Query(None),
    user: dict = Depends(current_user),
):
    tasks = task_service.list_tasks(
        user["id"],
        category=category or None,
        status=status.value if status else None,
        priority=priority.value if priority else None,
        deadline_before=deadline_before,
    )
    counts = comments_service.counts(user["id"])
    for t in tasks:
        c = counts.get(t["id"], {})
        t["comments_count"], t["files_count"] = c.get("comments", 0), c.get("files", 0)
    return tasks


@app.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: UUID, user: dict = Depends(current_user)):
    task = task_service.get_task(str(task_id), user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return task


@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: UUID, payload: TaskUpdate, user: dict = Depends(current_user)):
    updated = task_service.update_task(str(task_id), payload, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return updated


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: UUID, user: dict = Depends(current_user)):
    deleted = task_service.delete_task(str(task_id), user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return None


@app.get("/tasks/{task_id}/comments")
def list_task_comments(task_id: UUID, user: dict = Depends(current_user)):
    comments = comments_service.list_comments(str(task_id), user["id"])
    if comments is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return comments


@app.post("/tasks/{task_id}/comments", status_code=201)
def add_task_comment(task_id: UUID, payload: CommentCreate, user: dict = Depends(current_user)):
    """Комментарий: текст и/или файл (фото или PDF в base64, до 10 МБ)."""
    data = ftype = fname = None
    if payload.file:
        data, ftype = comments_service.decode_file(payload.file.data, payload.file.type)
        fname = payload.file.name
    comment = comments_service.add_comment(str(task_id), user["id"], payload.text, data, ftype, fname)
    if comment is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return comment


@app.delete("/comments/{comment_id}", status_code=204)
def delete_task_comment(comment_id: UUID, user: dict = Depends(current_user)):
    if not comments_service.delete_comment(str(comment_id), user["id"]):
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    return None


@app.get("/categories")
def list_categories(user: dict = Depends(current_user)):
    return categories_service.list_categories(user["id"])


@app.get("/categories/palette")
def category_palette():
    """Палитра цветов категорий (публичная: в ней нет данных пользователей)."""
    return {"colors": categories_service.PALETTE}


@app.post("/categories", status_code=201)
def create_category(payload: CategoryCreate, user: dict = Depends(current_user)):
    return categories_service.create_category(user["id"], payload.name, payload.color)


@app.put("/categories/order")
def reorder_categories(payload: CategoryOrder, user: dict = Depends(current_user)):
    return categories_service.reorder(user["id"], [str(i) for i in payload.ids])


@app.patch("/categories/{category_id}")
def update_category(category_id: UUID, payload: CategoryUpdate, user: dict = Depends(current_user)):
    return categories_service.update_category(str(category_id), user["id"], payload.name, payload.color)


@app.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: UUID, move_to: Optional[UUID] = Query(None), user: dict = Depends(current_user)):
    categories_service.delete_category(str(category_id), user["id"], str(move_to) if move_to else None)
    return None


# ---------- Пользователь ----------

@app.get("/me")
def get_me(user: dict = Depends(current_user)):
    return {"name": user.get("name"), "is_admin": bool(user.get("is_admin")),
            "timezone": user.get("timezone") or users_service.DEFAULT_TIMEZONE,
            "default_currency": money_service.default_currency(user["id"]),
            "currencies": list(money_service.CURRENCIES),
            "calendar_import": user.get("calendar_import") is not False,
            "remind_before": digest_service.remind_before(user)}


@app.patch("/me")
def update_me(payload: MeUpdate, background: BackgroundTasks, user: dict = Depends(current_user)):
    if payload.default_currency is not None:
        money_service.set_default_currency(user["id"], payload.default_currency)
    if payload.remind_before is not None:
        try:
            digest_service.set_remind_before(user["id"], payload.remind_before)
        except ValueError:
            raise HTTPException(status_code=422, detail="Можно 0, 15, 30 или 60 минут")
    if payload.calendar_import is not None:
        users_service.set_calendar_import(user["id"], payload.calendar_import)
        if payload.calendar_import:  # включили обратно — сразу подтянуть, что накопилось в календаре
            background.add_task(calendar_sync.sync_after_connect, user["id"])
    return get_me(users_service.get_by_id(user["id"]))


# ---------- Деньги (задача №3): всё через money_service, user_id только из current_user ----------

@app.get("/money/categories")
def list_money_categories(kind: Optional[str] = Query(None), user: dict = Depends(current_user)):
    return money_service.list_categories(user["id"], kind or None)


@app.post("/money/categories", status_code=201)
def create_money_category(payload: MoneyCategoryCreate, user: dict = Depends(current_user)):
    return money_service.create_category(user["id"], payload.kind, payload.name, payload.color)


@app.put("/money/categories/order")
def reorder_money_categories(payload: MoneyCategoryOrder, user: dict = Depends(current_user)):
    return money_service.reorder_categories(user["id"], payload.kind, [str(i) for i in payload.ids])


@app.patch("/money/categories/{category_id}")
def update_money_category(category_id: UUID, payload: MoneyCategoryUpdate, user: dict = Depends(current_user)):
    data = payload.model_dump(exclude_unset=True)
    extra = {"monthly_limit": data["monthly_limit"]} if "monthly_limit" in data else {}
    return money_service.update_category(str(category_id), user["id"], payload.name, payload.color, **extra)


@app.delete("/money/categories/{category_id}", status_code=204)
def delete_money_category(category_id: UUID, move_to: Optional[str] = Query(None), user: dict = Depends(current_user)):
    money_service.delete_category(str(category_id), user["id"], move_to or None)
    return None


@app.get("/money/transactions")
def list_money_transactions(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    kind: Optional[str] = Query(None),
    category_id: Optional[UUID] = Query(None),
    currency: Optional[str] = Query(None),
    limit: int = Query(money_service.LIST_LIMIT_MAX, ge=1, le=money_service.LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_user),
):
    return money_service.list_transactions(user["id"], date_from=date_from, date_to=date_to, kind=kind or None,
                                           category_id=str(category_id) if category_id else None,
                                           currency=currency or None, limit=limit, offset=offset)


@app.post("/money/transactions", status_code=201)
def create_money_transaction(payload: TransactionCreate, user: dict = Depends(current_user)):
    return money_service.create_transaction(user["id"], payload.model_dump(), source="app")


@app.get("/money/transactions/{tx_id}")
def get_money_transaction(tx_id: UUID, user: dict = Depends(current_user)):
    tx = money_service.get_transaction(str(tx_id), user["id"])
    if not tx:
        raise HTTPException(status_code=404, detail="Операция не найдена")
    return tx


@app.patch("/money/transactions/{tx_id}")
def update_money_transaction(tx_id: UUID, payload: TransactionUpdate, user: dict = Depends(current_user)):
    tx = money_service.update_transaction(str(tx_id), user["id"], payload.model_dump(exclude_unset=True))
    if not tx:
        raise HTTPException(status_code=404, detail="Операция не найдена")
    return tx


@app.delete("/money/transactions/{tx_id}", status_code=204)
def delete_money_transaction(tx_id: UUID, user: dict = Depends(current_user)):
    if not money_service.delete_transaction(str(tx_id), user["id"]):
        raise HTTPException(status_code=404, detail="Операция не найдена")
    return None


@app.post("/money/parse")
def parse_money(payload: MoneyParseRequest, user: dict = Depends(current_user)):
    """Черновики операций из текста — без сохранения: пользователь подтверждает их в форме."""
    try:
        drafts = money_service.drafts_from_text(user["id"], payload.text)
    except MoneyError:
        raise
    except Exception as exc:
        logger.warning("Разбор денег не удался: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail={"code": "parse_failed", "message": "Не получилось разобрать текст"})
    if not drafts:
        raise HTTPException(status_code=422, detail={"code": "no_amount", "message": "Не нашёл сумму, напишите её числом"})
    return {"items": drafts}


@app.post("/money/applepay", status_code=201)
def money_applepay(payload: ApplePayIn, user: dict = Depends(current_user)):
    """Покупка из Apple Pay (автоматизация «Транзакция» на iPhone, вход по личному ключу X-Access-Key).
    Бот пишет «💳 магазин · сумма · категория» с кнопками «Отменить» и «Категория»; повтор за 2 минуты не пишется."""
    import notifier

    tx, duplicate, how = money_service.record_applepay(user["id"], payload.amount, payload.merchant)
    cat = (money_service.get_category(tx["category_id"], user["id"]) or {}).get("name", "?")
    text = f"{tx['note']} · {tx['amount']} {tx['currency']} · {cat}"
    if not duplicate:
        hint = "\n(категория подобрана впервые — если не та, поправьте кнопкой, я запомню)" if how != "memory" else ""
        notifier.notify_user(user["id"], f"💳 Apple Pay: {text}{hint}", buttons=[[
            {"text": "Отменить", "callback_data": f"m:u:{tx['id']}"},
            {"text": "Категория", "callback_data": f"m:c:{tx['id']}"},
        ]])
    logger.info("Apple Pay: операция %s, категория из %s", tx["id"], how)
    return {"ok": True, "duplicate": duplicate, "text": text, "transaction": tx}


@app.get("/money/analytics")
def money_analytics(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                    user: dict = Depends(current_user)):
    """Итоги за любой период в основной валюте (по умолчанию текущий месяц) и сравнение с предыдущим таким же."""
    return money_service.analytics(user["id"], date_from, date_to)


@app.get("/money/summary")
def money_summary(month: Optional[str] = Query(None, description="YYYY-MM"), user: dict = Depends(current_user)):
    return money_service.summary(user["id"], month)


@app.get("/money/trend")
def money_trend(months: int = Query(6), user: dict = Depends(current_user)):
    return money_service.trend(user["id"], months)


@app.get("/money/recurring")
def list_money_recurring(kind: Optional[str] = Query(None), user: dict = Depends(current_user)):
    return money_service.list_recurring(user["id"], kind or None)


@app.post("/money/recurring", status_code=201)
def create_money_recurring(payload: RecurringCreate, user: dict = Depends(current_user)):
    return money_service.create_recurring(user["id"], payload.model_dump())


@app.patch("/money/recurring/{rec_id}")
def update_money_recurring(rec_id: UUID, payload: RecurringUpdate, user: dict = Depends(current_user)):
    rec = money_service.update_recurring(str(rec_id), user["id"], payload.model_dump(exclude_unset=True))
    if not rec:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    return rec


@app.delete("/money/recurring/{rec_id}", status_code=204)
def delete_money_recurring(rec_id: UUID, user: dict = Depends(current_user)):
    if not money_service.delete_recurring(str(rec_id), user["id"]):
        raise HTTPException(status_code=404, detail="Платёж не найден")
    return None


@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if not telegram_app or token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = TgUpdate.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


async def _send_telegram_document(client: httpx.AsyncClient, chat_id: int, filename: str, content: bytes,
                                  caption: str) -> bool:
    resp = await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                             data={"chat_id": str(chat_id), "caption": caption},
                             files={"document": (filename, content, "application/json")})
    return resp.status_code == 200


async def _send_telegram_message(client: httpx.AsyncClient, chat_id: int, text: str, buttons=None) -> bool:
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    resp = await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)
    return resp.status_code == 200


@app.post("/internal/reminders/run")
async def run_reminders(request: Request):
    """
    Рассылки по расписанию пользователя: утренняя (08:00) и вечерняя (20:00) сводка, напоминания о дедлайнах
    (13:00 и 17:00) с кнопками, отложенные задачи, срок которых наступил; до них — повторяющиеся платежи.
    Расписание считает digest_service по users.timezone; endpoint можно звать часто (cron-job.org каждые
    15 минут, плюс GitHub Actions) — каждая рассылка уходит один раз в своё окно (digest_log).
    Каждому только его задачи. Сбой у одного пользователя не мешает остальным. Защищено секретным заголовком.
    """
    if not REMINDERS_SECRET or request.headers.get("X-Reminder-Secret") != REMINDERS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN не настроен")

    sent = 0
    messages = 0
    recurring_messages = []
    calendar_messages = []
    try:  # события из Google Calendar → задачи, до сводок: утренняя сводка уже видит перенесённое
        calendar_messages = calendar_sync.run_all()
    except Exception as exc:
        logger.warning("Синхронизация календаря не прошла: %s", type(exc).__name__)
    try:
        recurring_messages = money_service.run_recurring()
    except Exception as exc:
        logger.warning("Повторяющиеся платежи не обработаны: %s", type(exc).__name__)
    async with httpx.AsyncClient(timeout=15) as client:
        for user_id, text in recurring_messages + calendar_messages:
            for chat_id in users_service.get_chat_ids(user_id):
                try:
                    await _send_telegram_message(client, chat_id, text)
                except Exception as exc:
                    logger.warning("Сообщение о платеже не отправлено: %s", type(exc).__name__)
        for user in users_service.list_users():
            try:
                due = digest_service.due_messages(user)
            except Exception as exc:
                logger.warning("Рассылка пользователю %s не собрана: %s", user["id"], type(exc).__name__)
                continue
            messages += len(due)
            for text, buttons in due:
                for chat_id in users_service.get_chat_ids(user["id"]):
                    try:
                        if await _send_telegram_message(client, chat_id, text, buttons):
                            sent += 1
                    except Exception as exc:
                        logger.warning("Сообщение не отправлено: %s", type(exc).__name__)
            try:  # воскресенье 21:00 — резервная копия данных пользователя в его чат
                if digest_service.backup_due(user):
                    name, content, count = backup_service.build(user["id"])
                    for chat_id in users_service.get_chat_ids(user["id"]):
                        await _send_telegram_document(client, chat_id, name, content, backup_service.caption(count))
            except Exception as exc:
                logger.warning("Резервная копия не отправлена: %s", type(exc).__name__)

    return {"sent": sent, "messages": messages, "recurring_notices": len(recurring_messages)}


@app.get("/health")
def health_check():
    """Публичная проверка: жив ли сервис, ходит ли он в базу и стоит ли webhook.
    Ответ всегда 200 (сбой базы не должен валить проверки Render), состояние базы — в поле "database".
    После перевыпуска ключей "database": "ok" значит, что новый SUPABASE_KEY на Render рабочий."""
    try:
        supabase.table("users").select("id").limit(1).execute()
        database = "ok"
    except Exception as exc:
        # Только тип ошибки: текст исключения может содержать адрес или фрагменты запроса.
        logger.warning("Проверка базы в /health не прошла: %s", type(exc).__name__)
        database = "error"
    return {"status": "ok", "database": database, "telegram_webhook": bool(telegram_app and WEBHOOK_BASE_URL)}


def _google_auth_url(user: dict) -> str:
    if not gcal.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Google OAuth не настроен: нет GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI",
        )
    if not gcal.OAUTH_STATE_SECRET:
        raise HTTPException(status_code=500, detail="Не задан OAUTH_STATE_SECRET")
    return gcal.build_auth_url(user["id"])


@app.get("/google/auth-url")
def google_auth_url(user: dict = Depends(current_user)):
    """Адрес страницы согласия Google для текущего пользователя. Отдельный маршрут нужен,
    потому что переход браузера по ссылке не передаёт заголовок входа."""
    return {"url": _google_auth_url(user)}


@app.get("/google/auth")
def google_auth_start(user: dict = Depends(current_user)):
    """Перенаправляет на страницу согласия Google (для клиентов, которые шлют заголовок входа)."""
    return RedirectResponse(_google_auth_url(user))


GOOGLE_DONE_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Google Calendar</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#FBFBFA;color:#17181C;font-family:system-ui,sans-serif;padding:24px;text-align:center}}
h1{{font-size:22px;margin:0 0 8px}}p{{color:#5D5F69;font-size:16px;margin:0}}</style></head>
<body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


@app.get("/google/callback")
def google_auth_callback(background: BackgroundTasks, code: Optional[str] = None, error: Optional[str] = None,
                         state: Optional[str] = None):
    """Google перенаправляет сюда после согласия. Кого подключаем — берём только из подписанного state."""
    if error:
        return HTMLResponse(GOOGLE_DONE_PAGE.format(
            title="Доступ не предоставлен", text="Можно закрыть эту вкладку и вернуться в Telegram."))
    user_id = gcal.verify_state(state)
    if not user_id or not users_service.get_by_id(user_id):
        return HTMLResponse(GOOGLE_DONE_PAGE.format(
            title="Ссылка устарела", text="Начните подключение заново из приложения."), status_code=400)
    if not code:
        raise HTTPException(status_code=400, detail="Отсутствует параметр code")
    gcal.exchange_code(code, user_id)
    background.add_task(calendar_sync.sync_after_connect, user_id)  # события календаря → задачи сразу
    return HTMLResponse(GOOGLE_DONE_PAGE.format(
        title="Готово ✅", text="Google Calendar подключён. Вернитесь в Telegram."))


@app.get("/google/calendars")
def google_calendars(user: dict = Depends(current_user)):
    """Календари пользователя и отметка, из каких переносятся события.
    needs_upgrade — подключение старое, без права видеть список: нужно переподключить календарь."""
    row = gcal._get_row(user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Google Calendar не подключён")
    selected = gcal.import_calendars(row)
    try:
        calendars = gcal.list_calendars(user["id"])
    except gcal.CalendarListUnavailable:
        return {"needs_upgrade": True, "calendars": []}
    except Exception as exc:
        logger.warning("Список календарей не получен: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Google Calendar не ответил, попробуйте позже")
    if calendars is None:
        raise HTTPException(status_code=409, detail="Google Calendar нужно переподключить")
    return {"needs_upgrade": False, "calendars": [{**c, "selected": c["id"] in selected} for c in calendars]}


@app.put("/google/calendars")
def choose_google_calendars(payload: CalendarChoice, user: dict = Depends(current_user)):
    """Сохранить выбор и сразу перенести события из новых календарей (ответ — сколько задач появилось)."""
    if not gcal._get_row(user["id"]):
        raise HTTPException(status_code=404, detail="Google Calendar не подключён")
    try:
        chosen = gcal.set_import_calendars(user["id"], payload.ids)
    except gcal.CalendarListUnavailable:
        raise HTTPException(status_code=409, detail="Переподключите Google Calendar, чтобы выбрать календари")
    except ValueError:
        raise HTTPException(status_code=422, detail="Нет такого календаря")
    created = 0
    if users_service.calendar_import_enabled(user["id"]):
        stats = calendar_sync.sync_user(user["id"]) or {}
        created = stats.get("created", 0)
    return {"selected": chosen, "created": created}


@app.get("/google/status")
def google_status(user: dict = Depends(current_user)):
    # {configured, connected, needs_reconnect}: токен проверяется вживую, результат кэшируется на 10 минут
    return gcal.status(user["id"])


@app.post("/notes", response_model=Note, status_code=201)
def create_note(payload: NoteCreate, user: dict = Depends(current_user)):
    return notes_service.create_note(payload, user["id"])


@app.get("/notes", response_model=list[Note])
def list_notes(user: dict = Depends(current_user)):
    return notes_service.list_notes(user["id"])


@app.patch("/notes/{note_id}", response_model=Note)
def update_note(note_id: UUID, payload: NoteUpdate, user: dict = Depends(current_user)):
    updated = notes_service.update_note(str(note_id), payload, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    return updated


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: UUID, user: dict = Depends(current_user)):
    deleted = notes_service.delete_note(str(note_id), user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    return None


@app.post("/tasks/{task_id}/subtasks", response_model=Subtask, status_code=201)
def create_subtask(task_id: UUID, payload: SubtaskCreate, user: dict = Depends(current_user)):
    created = subtasks_service.create_subtask(str(task_id), payload, user["id"])
    if not created:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return created


@app.get("/subtasks", response_model=list[Subtask])
def list_subtasks(user: dict = Depends(current_user)):
    return subtasks_service.list_all_subtasks(user["id"])


@app.patch("/subtasks/{subtask_id}", response_model=Subtask)
def update_subtask(subtask_id: UUID, payload: SubtaskUpdate, user: dict = Depends(current_user)):
    updated = subtasks_service.update_subtask(str(subtask_id), payload, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="Подзадача не найдена")
    return updated


@app.delete("/subtasks/{subtask_id}", status_code=204)
def delete_subtask(subtask_id: UUID, user: dict = Depends(current_user)):
    deleted = subtasks_service.delete_subtask(str(subtask_id), user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Подзадача не найдена")
    return None


# Раздаём веб-интерфейс с того же домена, что и API — без хардкода URL,
# без AirDrop, просто открываешь ссылку в любом браузере на любом устройстве.
# Смонтировано ПОСЛЕДНИМ: конкретные API-маршруты выше матчатся первыми,
# всё остальное (включая "/") отдаёт статику из папки web/.
_web_dir = os.path.join(os.path.dirname(__file__), "web")
if os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
