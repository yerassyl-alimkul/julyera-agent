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
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import task_service
import notes_service
import subtasks_service
import users_service
import google_calendar_service as gcal
from telegram_auth import InitDataError, verify_init_data
from models import (
    Category,
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


def current_user(
    x_telegram_init_data: Optional[str] = Header(None),
    x_access_key: Optional[str] = Header(None),
) -> dict:
    """Текущий пользователь из проверенного входа. user_id дальше берётся только отсюда."""
    if x_telegram_init_data:
        try:
            tg_user = verify_init_data(x_telegram_init_data, TELEGRAM_BOT_TOKEN or "")
        except InitDataError:
            raise HTTPException(status_code=401, detail="Неверные данные входа Telegram")
        user = users_service.get_by_telegram_id(tg_user["id"])
        if not user:
            raise HTTPException(
                status_code=403,
                detail={"code": "not_approved", "message": "Доступ ещё не одобрен владельцем"},
            )
        return user
    if x_access_key:
        user = users_service.get_by_access_key(x_access_key)
        if not user:
            raise HTTPException(status_code=401, detail="Неверный ключ доступа")
        return user
    raise HTTPException(status_code=401, detail="Нужен вход через Telegram")


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(payload: TaskCreate, user: dict = Depends(current_user)):
    return task_service.create_task(payload, user["id"])


@app.post("/tasks/parse", response_model=list[Task], status_code=201)
def parse_and_create_tasks(payload: ParseRequest, user: dict = Depends(current_user)):
    try:
        created = task_service.create_tasks_from_text(payload.text, user["id"], source_hint=payload.source)
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
    category: Optional[Category] = Query(None),
    status: Optional[Status] = Query(None),
    priority: Optional[Priority] = Query(None),
    deadline_before: Optional[datetime] = Query(None),
    user: dict = Depends(current_user),
):
    return task_service.list_tasks(
        user["id"],
        category=category.value if category else None,
        status=status.value if status else None,
        priority=priority.value if priority else None,
        deadline_before=deadline_before,
    )


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


@app.post("/telegram/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if not telegram_app or token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = TgUpdate.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


async def _send_telegram_message(client: httpx.AsyncClient, chat_id: int, text: str) -> bool:
    resp = await client.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    )
    return resp.status_code == 200


@app.post("/internal/reminders/run")
async def run_reminders(request: Request):
    """
    Каждому пользователю в его чаты — только его задачи с приближающимся/просроченным дедлайном.
    Вызывается по расписанию извне (см. GitHub Actions workflow .github/workflows/reminders.yml) —
    сам backend расписание не хранит. Защищено секретным заголовком.
    """
    if not REMINDERS_SECRET or request.headers.get("X-Reminder-Secret") != REMINDERS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN не настроен")

    from telegram_handlers import format_reminder_message

    sent = 0
    tasks_found = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for user in users_service.list_users():
            tasks = task_service.get_reminder_tasks(user["id"])
            tasks_found += len(tasks)
            if not tasks:
                continue
            message = format_reminder_message(tasks)
            for chat_id in users_service.get_chat_ids(user["id"]):
                if await _send_telegram_message(client, chat_id, message):
                    sent += 1

    return {"sent": sent, "tasks_found": tasks_found}


@app.get("/health")
def health_check():
    return {"status": "ok", "telegram_webhook": bool(telegram_app and WEBHOOK_BASE_URL)}


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
def google_auth_callback(code: Optional[str] = None, error: Optional[str] = None, state: Optional[str] = None):
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
    return HTMLResponse(GOOGLE_DONE_PAGE.format(
        title="Готово ✅", text="Google Calendar подключён. Вернитесь в Telegram."))


@app.get("/google/status")
def google_status(user: dict = Depends(current_user)):
    return {"connected": gcal.is_connected(user["id"]), "configured": gcal.is_configured()}


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
