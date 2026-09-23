"""
Общая логика работы с задачами: используется и HTTP-эндпоинтами (main.py),
и Telegram-хендлерами (telegram_handlers.py) — без лишнего HTTP-круга
внутри одного процесса, когда бот работает в режиме webhook.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from database import supabase
from groq_client import parse_text_to_tasks
from models import Status, TaskCreate, TaskUpdate

TABLE = "tasks"


def _next_deadline(current: Optional[date], pattern: Optional[str]) -> Optional[date]:
    base = current or date.today()
    if pattern == "daily":
        return base + timedelta(days=1)
    if pattern == "weekly":
        return base + timedelta(weeks=1)
    if pattern == "monthly":
        return base + timedelta(days=30)
    return None


def create_task(payload: TaskCreate) -> dict:
    data = payload.model_dump(mode="json", exclude_none=False)
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать задачу")
    return result.data[0]


def create_tasks_from_text(raw_text: str, source_hint: Optional[str] = None) -> list[dict]:
    raw_tasks = parse_text_to_tasks(raw_text, source_hint=source_hint)
    created = []
    for raw in raw_tasks:
        try:
            validated = TaskCreate(**raw)
        except Exception:
            # Пропускаем некорректный элемент от LLM, не роняем остальные
            continue
        created.append(create_task(validated))
    return created


def list_tasks(
    category: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    deadline_before: Optional[date] = None,
) -> list[dict]:
    query = supabase.table(TABLE).select("*")
    if category:
        query = query.eq("category", category)
    if status:
        query = query.eq("status", status)
    if priority:
        query = query.eq("priority", priority)
    if deadline_before:
        query = query.lte("deadline", deadline_before.isoformat())
    result = query.order("deadline", desc=False, nullsfirst=False).execute()
    return result.data


def get_task(task_id: str) -> Optional[dict]:
    result = supabase.table(TABLE).select("*").eq("id", task_id).execute()
    return result.data[0] if result.data else None


def update_task(task_id: str, payload: TaskUpdate) -> Optional[dict]:
    current = get_task(task_id)
    if not current:
        return None

    update_data = payload.model_dump(mode="json", exclude_none=True)
    update_data["updated_at"] = datetime.utcnow().isoformat()

    result = supabase.table(TABLE).update(update_data).eq("id", task_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить задачу")
    updated = result.data[0]

    # Повторяющиеся задачи: закрыли — создаём следующую с пересчитанным дедлайном
    became_done = update_data.get("status") == Status.done.value
    if became_done and current.get("is_recurring"):
        next_deadline = _next_deadline(
            current.get("deadline") and date.fromisoformat(current["deadline"]),
            current.get("recurrence_pattern"),
        )
        new_task = {
            "task": current["task"],
            "category": current["category"],
            "source": current.get("source"),
            "deadline": next_deadline.isoformat() if next_deadline else None,
            "priority": current["priority"],
            "status": Status.todo.value,
            "note": current.get("note"),
            "is_recurring": True,
            "recurrence_pattern": current.get("recurrence_pattern"),
        }
        supabase.table(TABLE).insert(new_task).execute()

    return updated


def delete_task(task_id: str) -> bool:
    result = supabase.table(TABLE).delete().eq("id", task_id).execute()
    return bool(result.data)


def upsert_telegram_chat(chat_id: int) -> None:
    """Запоминает chat_id, чтобы потом можно было слать туда напоминания."""
    supabase.table("telegram_chats").upsert({"chat_id": chat_id}).execute()


def get_telegram_chat_ids() -> list[int]:
    result = supabase.table("telegram_chats").select("chat_id").execute()
    return [row["chat_id"] for row in result.data]


def get_reminder_tasks(days_ahead: int = 2) -> list[dict]:
    """
    Задачи, которые не закрыты и у которых дедлайн уже прошёл или наступает
    в течение ближайших `days_ahead` дней (по умолчанию 2 — сегодня/завтра/послезавтра).
    """
    cutoff = date.today() + timedelta(days=days_ahead)
    result = []
    for t in list_tasks():
        if t.get("status") == Status.done.value:
            continue
        deadline = t.get("deadline")
        if not deadline:
            continue
        if date.fromisoformat(deadline) <= cutoff:
            result.append(t)
    result.sort(key=lambda t: t["deadline"])
    return result
