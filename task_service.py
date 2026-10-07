"""
Общая логика работы с задачами: используется и HTTP-эндпоинтами (main.py),
и Telegram-хендлерами (telegram_handlers.py) — без лишнего HTTP-круга
внутри одного процесса, когда бот работает в режиме webhook.

Каждая функция получает user_id владельца задач, и каждый запрос к таблице tasks
фильтруется по нему: backend ходит в базу с ключом service_role, другой защиты нет.
"""
from datetime import datetime, timedelta
from typing import Optional

import categories_service
import google_calendar_service as gcal
import users_service
from categories_service import CategoryError
from database import supabase
from groq_client import parse_text_to_tasks
from models import Status, TaskCreate, TaskUpdate

TABLE = "tasks"
NULLABLE_FIELDS = {"deadline", "note", "source", "recurrence_pattern"}


def _next_deadline(current: Optional[datetime], pattern: Optional[str]) -> Optional[datetime]:
    base = current or datetime.now()
    if pattern == "daily":
        return base + timedelta(days=1)
    if pattern == "weekly":
        return base + timedelta(weeks=1)
    if pattern == "monthly":
        return base + timedelta(days=30)
    return None


def _set_event_id(task: dict, user_id: str, event_id: Optional[str]) -> None:
    supabase.table(TABLE).update({"google_event_id": event_id}).eq("id", task["id"]).eq("user_id", user_id).execute()
    task["google_event_id"] = event_id


def _sync_google_upsert(task: dict, user_id: str) -> dict:
    """Создаёт/обновляет событие в Google Calendar пользователя и, если id изменился,
    сохраняет его в самой задаче. Возвращает актуальную версию задачи."""
    event_id = gcal.sync_task_upsert(task, user_id)
    if event_id != task.get("google_event_id"):
        _set_event_id(task, user_id, event_id)
    return task


def _sync_google_clear(task: dict, user_id: str) -> dict:
    """Удаляет событие из Google Calendar пользователя (если было) и чистит ссылку на него."""
    gcal.sync_task_delete(task.get("google_event_id"), user_id)
    if task.get("google_event_id"):
        _set_event_id(task, user_id, None)
    return task


def _known_category(user_id: str, name: str) -> str:
    """Название категории в том виде, как оно у пользователя; чужое или несуществующее — 422."""
    resolved = categories_service.resolve_name(user_id, name)
    if not resolved:
        raise CategoryError(422, f"Нет такой категории: {name}", "unknown_category")
    return resolved


def create_task(payload: TaskCreate, user_id: str) -> dict:
    data = payload.model_dump(mode="json", exclude_none=False)
    data["user_id"] = user_id  # владельца ставит сервер, не клиент
    data["category"] = _known_category(user_id, data["category"])
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать задачу")
    task = result.data[0]
    return _sync_google_upsert(task, user_id)


def create_tasks_from_text(raw_text: str, user_id: str, source_hint: Optional[str] = None) -> list[dict]:
    names = categories_service.names(user_id) or [categories_service.fallback_name(user_id)]
    raw_tasks = parse_text_to_tasks(raw_text, names, source_hint=source_hint,
                                    timezone=users_service.timezone_of(user_id))
    fallback = names[0]
    created = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        # Модель могла вернуть чужое или выдуманное название — подставляем запасную категорию,
        # парсер не должен падать из-за категории.
        raw = {**raw, "category": categories_service.resolve_name(user_id, str(raw.get("category") or "")) or fallback}
        try:
            validated = TaskCreate(**raw)
        except Exception:
            # Пропускаем некорректный элемент от LLM, не роняем остальные
            continue
        created.append(create_task(validated, user_id))
    return created


def list_tasks(
    user_id: str,
    category: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    deadline_before: Optional[datetime] = None,
) -> list[dict]:
    query = supabase.table(TABLE).select("*").eq("user_id", user_id)
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


def get_task(task_id: str, user_id: str) -> Optional[dict]:
    result = supabase.table(TABLE).select("*").eq("id", task_id).eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def update_task(task_id: str, payload: TaskUpdate, user_id: str) -> Optional[dict]:
    current = get_task(task_id, user_id)
    if not current:
        return None

    # Берём только переданные поля. Явный null разрешён там, где колонка может быть пустой
    # (так снимается дедлайн); для остальных полей null игнорируется.
    update_data = {
        key: value
        for key, value in payload.model_dump(mode="json", exclude_unset=True).items()
        if value is not None or key in NULLABLE_FIELDS
    }
    if "category" in update_data:
        update_data["category"] = _known_category(user_id, update_data["category"])
    update_data["updated_at"] = datetime.utcnow().isoformat()

    result = supabase.table(TABLE).update(update_data).eq("id", task_id).eq("user_id", user_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить задачу")
    updated = result.data[0]

    # Повторяющиеся задачи: закрыли — создаём следующую у того же пользователя
    became_done = update_data.get("status") == Status.done.value
    if became_done and current.get("is_recurring"):
        next_deadline = _next_deadline(
            current.get("deadline") and datetime.fromisoformat(current["deadline"]),
            current.get("recurrence_pattern"),
        )
        new_task = {
            "user_id": user_id,
            "task": current["task"],
            "category": current["category"],
            "source": current.get("source"),
            "deadline": next_deadline.isoformat() if next_deadline else None,
            "priority": current["priority"],
            "status": Status.todo.value,
            "note": current.get("note"),
            "is_recurring": True,
            "recurrence_pattern": current.get("recurrence_pattern"),
            "tags": current.get("tags") or [],
        }
        inserted = supabase.table(TABLE).insert(new_task).execute()
        if inserted.data:
            _sync_google_upsert(inserted.data[0], user_id)

    # Синхронизация с Google Calendar: закрыли задачу — убираем событие из
    # календаря; иначе, если есть дедлайн, создаём/обновляем событие;
    # если дедлайн убрали — тоже убираем событие.
    if became_done:
        updated = _sync_google_clear(updated, user_id)
    elif updated.get("deadline"):
        updated = _sync_google_upsert(updated, user_id)
    elif updated.get("google_event_id"):
        updated = _sync_google_clear(updated, user_id)

    return updated


def delete_task(task_id: str, user_id: str) -> bool:
    task = get_task(task_id, user_id)
    if not task:
        return False
    gcal.sync_task_delete(task.get("google_event_id"), user_id)
    result = supabase.table(TABLE).delete().eq("id", task_id).eq("user_id", user_id).execute()
    return bool(result.data)


def get_reminder_tasks(user_id: str, days_ahead: int = 2) -> list[dict]:
    """
    Задачи пользователя, которые не закрыты и у которых дедлайн уже прошёл или наступает
    в течение ближайших `days_ahead` дней (по умолчанию 2 — сегодня/завтра/послезавтра).
    Сравнение идёт по дате (без учёта времени суток), чтобы задача "сегодня в 23:59"
    и "сегодня в 09:00" одинаково считались "сегодняшними".
    """
    cutoff = datetime.now().date() + timedelta(days=days_ahead)
    result = []
    for t in list_tasks(user_id):
        if t.get("status") == Status.done.value:
            continue
        deadline = t.get("deadline")
        if not deadline:
            continue
        if datetime.fromisoformat(deadline).date() <= cutoff:
            result.append(t)
    result.sort(key=lambda t: t["deadline"])
    return result
