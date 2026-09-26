"""
Backend для персонального агента структурирования задач.

Эндпоинты:
    POST   /tasks                    — создать задачу (структурированно)
    POST   /tasks/parse              — разобрать сырой текст через Groq и создать задачи
    GET    /tasks                    — список задач (с фильтрами)
    GET    /tasks/{id}               — одна задача
    PATCH  /tasks/{id}                — обновить задачу
    DELETE /tasks/{id}               — удалить задачу
    POST   /telegram/webhook/{token} — приём обновлений от Telegram (продакшен)

Локальный запуск:
    uvicorn main:app --reload

В продакшене (Render и т.п.) задай WEBHOOK_BASE_URL — при старте
приложение само зарегистрирует webhook в Telegram.
Локально WEBHOOK_BASE_URL не задавай — вместо этого используй bot.py (polling).
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import task_service
import notes_service
import google_calendar_service as gcal
from models import Category, ParseRequest, Priority, Status, Task, TaskCreate, TaskUpdate, Note, NoteCreate, NoteUpdate

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")  # например https://task-agent.onrender.com
REMINDERS_SECRET = os.environ.get("REMINDERS_SECRET")  # секрет для /internal/reminders/run

telegram_app = None
if TELEGRAM_BOT_TOKEN:
    from telegram import Update as TgUpdate
    from telegram.ext import Application as TgApplication, CommandHandler, MessageHandler, filters

    from telegram_handlers import handle_text, list_tasks_handler, start

    telegram_app = TgApplication.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("list", list_tasks_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if telegram_app:
        await telegram_app.initialize()
        if WEBHOOK_BASE_URL:
            webhook_url = f"{WEBHOOK_BASE_URL}/telegram/webhook/{TELEGRAM_BOT_TOKEN}"
            await telegram_app.bot.set_webhook(webhook_url)
    yield
    if telegram_app:
        await telegram_app.shutdown()


app = FastAPI(title="Task Agent API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(payload: TaskCreate):
    return task_service.create_task(payload)


@app.post("/tasks/parse", response_model=list[Task], status_code=201)
def parse_and_create_tasks(payload: ParseRequest):
    try:
        created = task_service.create_tasks_from_text(payload.text, source_hint=payload.source)
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
):
    return task_service.list_tasks(
        category=category.value if category else None,
        status=status.value if status else None,
        priority=priority.value if priority else None,
        deadline_before=deadline_before,
    )


@app.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: UUID):
    task = task_service.get_task(str(task_id))
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return task


@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: UUID, payload: TaskUpdate):
    updated = task_service.update_task(str(task_id), payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return updated


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: UUID):
    deleted = task_service.delete_task(str(task_id))
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


@app.post("/internal/reminders/run")
async def run_reminders(request: Request):
    """
    Проверяет задачи с приближающимся/просроченным дедлайном и шлёт
    напоминание в Telegram. Вызывается по расписанию извне (см. GitHub Actions
    workflow .github/workflows/reminders.yml) — сам backend расписание не хранит.
    Защищено секретным заголовком, чтобы эндпоинт не мог дёргать кто угодно.
    """
    if not REMINDERS_SECRET or request.headers.get("X-Reminder-Secret") != REMINDERS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN не настроен")

    from telegram_handlers import format_reminder_message

    tasks = task_service.get_reminder_tasks()
    chat_ids = task_service.get_telegram_chat_ids()

    if not tasks or not chat_ids:
        return {"sent": 0, "tasks_found": len(tasks)}

    message = format_reminder_message(tasks)
    sent = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for chat_id in chat_ids:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
            if resp.status_code == 200:
                sent += 1

    return {"sent": sent, "tasks_found": len(tasks)}


@app.get("/health")
def health_check():
    return {"status": "ok", "telegram_webhook": bool(telegram_app and WEBHOOK_BASE_URL)}


@app.get("/google/auth")
def google_auth_start():
    """Перенаправляет на страницу согласия Google для подключения календаря."""
    if not gcal.is_configured():
        raise HTTPException(
            status_code=500,
            detail="Google OAuth не настроен: нет GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI",
        )
    return RedirectResponse(gcal.build_auth_url())


@app.get("/google/callback")
def google_auth_callback(code: Optional[str] = None, error: Optional[str] = None):
    """Google перенаправляет сюда после согласия пользователя."""
    if error:
        return HTMLResponse(f"<h3>Google Calendar: доступ не предоставлен ({error})</h3>")
    if not code:
        raise HTTPException(status_code=400, detail="Отсутствует параметр code")
    gcal.exchange_code(code)
    return HTMLResponse(
        "<h3>Google Calendar подключён ✅</h3><p>Можешь закрыть эту вкладку и вернуться в приложение.</p>"
    )


@app.get("/google/status")
def google_status():
    return {"connected": gcal.is_connected(), "configured": gcal.is_configured()}


@app.post("/notes", response_model=Note, status_code=201)
def create_note(payload: NoteCreate):
    return notes_service.create_note(payload)


@app.get("/notes", response_model=list[Note])
def list_notes():
    return notes_service.list_notes()


@app.patch("/notes/{note_id}", response_model=Note)
def update_note(note_id: UUID, payload: NoteUpdate):
    updated = notes_service.update_note(str(note_id), payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    return updated


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: UUID):
    deleted = notes_service.delete_note(str(note_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    return None


# Раздаём веб-интерфейс с того же домена, что и API — без хардкода URL,
# без AirDrop, просто открываешь ссылку в любом браузере на любом устройстве.
# Смонтировано ПОСЛЕДНИМ: конкретные API-маршруты выше матчатся первыми,
# всё остальное (включая "/") отдаёт статику из папки web/.
import os as _os

_web_dir = _os.path.join(_os.path.dirname(__file__), "web")
if _os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
