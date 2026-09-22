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
from datetime import date
from typing import Optional
from uuid import UUID

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv()

import task_service
from models import Category, ParseRequest, Priority, Status, Task, TaskCreate, TaskUpdate

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")  # например https://task-agent.onrender.com

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
    deadline_before: Optional[date] = Query(None),
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


@app.get("/health")
def health_check():
    return {"status": "ok", "telegram_webhook": bool(telegram_app and WEBHOOK_BASE_URL)}


# Раздаём веб-интерфейс с того же домена, что и API — без хардкода URL,
# без AirDrop, просто открываешь ссылку в любом браузере на любом устройстве.
# Смонтировано ПОСЛЕДНИМ: конкретные API-маршруты выше матчатся первыми,
# всё остальное (включая "/") отдаёт статику из папки web/.
import os as _os

_web_dir = _os.path.join(_os.path.dirname(__file__), "web")
if _os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
