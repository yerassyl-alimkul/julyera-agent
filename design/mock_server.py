"""
Локальный сервер с подставными данными для проверки интерфейса без настоящей базы.
Раздаёт web/ и имитирует API (задачи, подзадачи, заметки, статус Google) в памяти.
Данные сбрасываются при перезапуске. Даты строятся от сегодняшнего дня.

Запуск:
    venv/bin/python design/mock_server.py      # http://localhost:8001
"""
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

app = FastAPI(title="Task Agent mock")

# Вход упрощён: заголовки не проверяются, кроме особых ключей для проверки экранов входа.
#   X-Access-Key: expired → 401 (как неверный ключ), pending → 403 not_approved.
# /__mock/last-auth говорит, какой заголовок входа пришёл последним (только вид, без значения).
LAST_AUTH = {"kind": None}
API_PREFIXES = ("/tasks", "/subtasks", "/notes", "/google/status", "/google/auth-url")


@app.middleware("http")
async def fake_auth(request: Request, call_next):
    if request.url.path.startswith(API_PREFIXES):
        key = request.headers.get("x-access-key")
        LAST_AUTH["kind"] = ("init-data" if request.headers.get("x-telegram-init-data")
                             else "access-key" if key else None)
        if key == "expired":
            return JSONResponse({"detail": "Неверный ключ доступа"}, status_code=401)
        if key == "pending":
            return JSONResponse({"detail": {"code": "not_approved", "message": "Доступ ещё не одобрен"}},
                                status_code=403)
    return await call_next(request)


@app.get("/__mock/last-auth")
def last_auth():
    return LAST_AUTH


@app.get("/google/auth-url")
def google_auth_url():
    return {"url": "https://accounts.google.com/o/oauth2/auth?state=mock"}

TASKS: dict[str, dict] = {}
SUBTASKS: dict[str, dict] = {}
NOTES: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deadline(days: int, hhmm: str) -> str:
    # Как Supabase: «голые» часы с ложной меткой +00:00.
    return f"{(date.today() + timedelta(days=days)).isoformat()}T{hhmm}:00+00:00"


def _add_task(task, category, days=None, hhmm="23:59", priority="Mid", status="To Do",
              tags=None, note=None, steps=None, recurring=None):
    tid = str(uuid.uuid4())
    TASKS[tid] = {
        "id": tid, "task": task, "category": category, "source": None,
        "deadline": _deadline(days, hhmm) if days is not None else None,
        "priority": priority, "status": status, "note": note,
        "is_recurring": bool(recurring), "recurrence_pattern": recurring,
        "tags": tags or [], "google_event_id": None,
        "created_at": _now(), "updated_at": _now(),
    }
    for text, done in steps or []:
        sid = str(uuid.uuid4())
        SUBTASKS[sid] = {"id": sid, "task_id": tid, "content": text, "is_done": done, "created_at": _now()}
    return tid


def seed() -> None:
    _add_task("Сдать отчёт по продажам за сентябрь", "Work", -2, "18:00", "High", tags=["отчёт"])
    _add_task("Записаться к стоматологу", "Personal", -1, "10:00", "Mid")
    _add_task("Лабораторная по статистике: регрессия и проверка гипотез", "UniMe", 0, "09:00", "High",
              tags=["экзамен", "статистика"],
              steps=[("Скачать датасет", True), ("Построить модель", False), ("Оформить выводы", False)])
    _add_task("Созвон с командой", "Work", 0, "14:00", "Mid", recurring="weekly")
    _add_task("Пройти урок SQL: оконные функции", "Study", 0, "19:00", "Low", tags=["sql"])
    _add_task("Купить продукты", "Personal", 0, "21:00", "Low")
    _add_task("Подготовить презентацию для клиента", "Work", 1, "12:00", "High",
              steps=[("Собрать цифры", False), ("Слайды", False)])
    _add_task("Прочитать главу 4 по эконометрике", "UniMe", 2, "23:59", "Mid", tags=["чтение"])
    _add_task("Курс по Power BI, модуль 2", "Study", 4, "20:00", "Low")
    _add_task("Оплатить аренду", "Personal", 6, "12:00", "High", recurring="monthly")
    _add_task("Экзамен по машинному обучению", "UniMe", 12, "09:30", "High", tags=["экзамен"])
    _add_task("Разобрать почту", "Work", None, priority="Low")
    _add_task("Найти курс по испанскому", "Study", None, priority="Mid", tags=["язык"])
    _add_task("Отправить документы в универ", "UniMe", -3, "17:00", "Mid", status="Done")
    _add_task("Обновить резюме", "Personal", -5, "23:59", "Low", status="Done")
    for text in ["Идея: разобрать силлабус целиком на задачи",
                 "Пароль от Wi-Fi в библиотеке спросить у администратора"]:
        nid = str(uuid.uuid4())
        NOTES[nid] = {"id": nid, "content": text, "created_at": _now(), "updated_at": _now()}


def _get(store: dict, key: str) -> dict:
    if key not in store:
        raise HTTPException(404, "Не найдено")
    return store[key]


@app.get("/health")
def health():
    return {"status": "ok", "telegram_webhook": False}


@app.get("/google/status")
def google_status():
    return {"connected": False, "configured": True}


@app.get("/tasks")
def list_tasks(category: Optional[str] = None, status: Optional[str] = None, priority: Optional[str] = None):
    items = [t for t in TASKS.values()
             if (not category or t["category"] == category)
             and (not status or t["status"] == status)
             and (not priority or t["priority"] == priority)]
    return sorted(items, key=lambda t: (t["deadline"] is None, t["deadline"] or ""))


@app.post("/tasks", status_code=201)
async def create_task(request: Request):
    body = await request.json()
    tid = _add_task(body["task"], body.get("category", "Personal"), priority=body.get("priority", "Mid"))
    TASKS[tid].update({k: v for k, v in body.items() if k in TASKS[tid]})
    return TASKS[tid]


@app.post("/tasks/parse", status_code=201)
async def parse_tasks(request: Request):
    # Без LLM: каждая непустая строка становится задачей; «завтра» в тексте ставит дедлайн на завтра.
    body = await request.json()
    created = []
    for line in [l.strip() for l in body["text"].splitlines() if l.strip()]:
        days = 1 if "завтра" in line.lower() else None
        tags = [w[1:] for w in line.split() if w.startswith("#") and len(w) > 1]
        created.append(TASKS[_add_task(line, "Personal", days, "23:59", "Mid", tags=tags)])
    if not created:
        raise HTTPException(422, "Не удалось извлечь ни одной подходящей задачи из текста")
    return created


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    return _get(TASKS, task_id)


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, request: Request):
    task = _get(TASKS, task_id)
    body = await request.json()
    became_done = body.get("status") == "Done" and task["status"] != "Done"
    task.update({k: v for k, v in body.items() if k in task})
    task["updated_at"] = _now()
    if became_done and task["is_recurring"] and task["deadline"]:
        step = {"daily": 1, "weekly": 7, "monthly": 30}.get(task["recurrence_pattern"], 0)
        current = datetime.fromisoformat(task["deadline"]).replace(tzinfo=None)
        nid = _add_task(task["task"], task["category"], priority=task["priority"],
                        tags=list(task["tags"]), recurring=task["recurrence_pattern"])
        TASKS[nid]["deadline"] = (current + timedelta(days=step)).isoformat() + "+00:00"
    return task


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: str):
    _get(TASKS, task_id)
    del TASKS[task_id]
    for sid in [s for s, v in SUBTASKS.items() if v["task_id"] == task_id]:
        del SUBTASKS[sid]


@app.get("/subtasks")
def list_subtasks():
    return sorted(SUBTASKS.values(), key=lambda s: s["created_at"])


@app.post("/tasks/{task_id}/subtasks", status_code=201)
async def create_subtask(task_id: str, request: Request):
    _get(TASKS, task_id)
    body = await request.json()
    sid = str(uuid.uuid4())
    SUBTASKS[sid] = {"id": sid, "task_id": task_id, "content": body["content"], "is_done": False, "created_at": _now()}
    return SUBTASKS[sid]


@app.patch("/subtasks/{subtask_id}")
async def update_subtask(subtask_id: str, request: Request):
    sub = _get(SUBTASKS, subtask_id)
    sub.update({k: v for k, v in (await request.json()).items() if k in ("content", "is_done") and v is not None})
    return sub


@app.delete("/subtasks/{subtask_id}", status_code=204)
def delete_subtask(subtask_id: str):
    _get(SUBTASKS, subtask_id)
    del SUBTASKS[subtask_id]


@app.get("/notes")
def list_notes():
    return sorted(NOTES.values(), key=lambda n: n["created_at"], reverse=True)


@app.post("/notes", status_code=201)
async def create_note(request: Request):
    nid = str(uuid.uuid4())
    NOTES[nid] = {"id": nid, "content": (await request.json())["content"], "created_at": _now(), "updated_at": _now()}
    return NOTES[nid]


@app.patch("/notes/{note_id}")
async def update_note(note_id: str, request: Request):
    note = _get(NOTES, note_id)
    note.update({"content": (await request.json())["content"], "updated_at": _now()})
    return note


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str):
    _get(NOTES, note_id)
    del NOTES[note_id]


# Статика последней, как в main.py.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

seed()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8001)))
