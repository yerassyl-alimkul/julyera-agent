"""
Общая логика работы с задачами: используется и HTTP-эндпоинтами (main.py),
и Telegram-хендлерами (telegram_handlers.py) — без лишнего HTTP-круга
внутри одного процесса, когда бот работает в режиме webhook.

Каждая функция получает user_id владельца задач, и каждый запрос к таблице tasks
фильтруется по нему: backend ходит в базу с ключом service_role, другой защиты нет.
"""
from datetime import date, datetime, timedelta
from typing import Optional

import categories_service
import google_calendar_service as gcal
import users_service
from categories_service import CategoryError
from database import supabase
from groq_client import parse_text_to_tasks
from models import Status, TaskCreate, TaskUpdate

TABLE = "tasks"
NULLABLE_FIELDS = {"deadline", "ends_at", "note", "source", "recurrence_pattern", "recurrence_days"}
# Повторения одного события Google Calendar связаны: эти поля, изменённые у одного, меняются у всех незакрытых.
# Время, статус и удаление — только у самого повторения (как «только это событие» в Google).
SERIES_FIELDS = ("task", "category", "priority", "note")


def _clamp_day(year: int, month: int, day: int) -> date:
    """31-е в коротком месяце (и 29 февраля в обычном году) — последний день месяца."""
    import calendar
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _step(d: datetime, pattern: str, days: Optional[list]) -> Optional[datetime]:
    if pattern == "daily":
        return d + timedelta(days=1)
    if pattern == "weekly":
        return d + timedelta(weeks=1)
    if pattern in ("weekdays", "days"):
        allowed = set(range(5)) if pattern == "weekdays" else set(days or [d.weekday()])
        nxt = d + timedelta(days=1)
        while nxt.weekday() not in allowed:
            nxt += timedelta(days=1)
        return nxt
    if pattern == "monthly":
        anchor = (days or [d.day])[0]
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return datetime.combine(_clamp_day(y, m, anchor), d.time())
    if pattern == "yearly":
        anchor = (days or [d.day])[0]
        return datetime.combine(_clamp_day(d.year + 1, d.month, anchor), d.time())
    return None


def next_occurrence(current: Optional[datetime], pattern: Optional[str], days: Optional[list] = None,
                    today: Optional[date] = None) -> Optional[datetime]:
    """Следующий срок повторяющейся задачи. Закрыли с опозданием — пропускаем прошедшие повторы:
    следующий срок всегда позже сегодняшнего дня. Без дедлайна — от «сейчас»."""
    if not pattern:
        return None
    base = current.replace(tzinfo=None) if current else datetime.now()
    today = today or datetime.now().date()
    nxt = _step(base, pattern, days)
    for _ in range(800):
        if nxt is None or nxt.date() > today:
            break
        nxt = _step(nxt, pattern, days)
    return nxt


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=None)


def _normalize_ends(data: dict, current: Optional[dict] = None) -> None:
    """«с — до» (migration 012): ends_at только при дедлайне и позже него, иначе 422. Перенесли задачу, не трогая
    «до» (кнопки «На завтра», перетаскивание в календаре), — конец едет вместе с началом, длительность та же.
    Сняли дедлайн — снимается и конец."""
    current = current or {}
    start = _dt(data["deadline"]) if "deadline" in data else _dt(current.get("deadline"))
    if "ends_at" in data:
        end = _dt(data["ends_at"])
    elif "deadline" in data and current.get("ends_at") and current.get("deadline") and start:
        end = start + (_dt(current["ends_at"]) - _dt(current["deadline"]))
    else:
        return
    if end and not start:
        end = None
    if end and end <= start:
        raise CategoryError(422, "Время «до» должно быть позже начала", "bad_ends_at")
    data["ends_at"] = end.isoformat() if end else None


def _normalize_recurrence(data: dict, deadline_value) -> None:
    """is_recurring и recurrence_days из recurrence_pattern: дни недели 0–6 для «days», число месяца для
    «monthly»/«yearly» (из дедлайна, если не передано). Неверные дни — 422."""
    if "recurrence_pattern" not in data and "recurrence_days" not in data:
        return
    pattern = data.get("recurrence_pattern")
    days = data.get("recurrence_days")
    data["is_recurring"] = bool(pattern)
    if pattern == "days":
        days = sorted({int(x) for x in (days or []) if isinstance(x, int) and 0 <= x <= 6})
        if not days:
            raise CategoryError(422, "Выберите хотя бы один день недели", "bad_recurrence")
        data["recurrence_days"] = days
    elif pattern in ("monthly", "yearly"):
        deadline = deadline_value if isinstance(deadline_value, datetime) else (
            datetime.fromisoformat(deadline_value) if deadline_value else None)
        day = (days or [None])[0] if days else (deadline.day if deadline else None)
        data["recurrence_days"] = [day] if isinstance(day, int) and 1 <= day <= 31 else None
    else:
        data["recurrence_days"] = None


def _next_deadline(current: Optional[datetime], pattern: Optional[str], days: Optional[list] = None,
                   user_id: Optional[str] = None) -> Optional[datetime]:
    today = None
    if user_id:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(users_service.timezone_of(user_id))).date()
    return next_occurrence(current, pattern, days, today)


def _set_event_id(task: dict, user_id: str, event_id: Optional[str], extra: Optional[dict] = None) -> None:
    data = {"google_event_id": event_id, **(extra or {})}
    supabase.table(TABLE).update(data).eq("id", task["id"]).eq("user_id", user_id).execute()
    task.update(data)


def _sync_google_upsert(task: dict, user_id: str) -> dict:
    """Создаёт/обновляет событие в Google Calendar пользователя и сохраняет в задаче его id и время правки
    (gcal_updated: по нему calendar_sync понимает, что событие с тех пор меняли в Google). Возвращает задачу."""
    event_id, updated = gcal.sync_task_upsert_ex(task, user_id)
    if event_id != task.get("google_event_id") or updated:
        _set_event_id(task, user_id, event_id, {"gcal_updated": updated} if updated else None)
    return task


def _sync_google_clear(task: dict, user_id: str) -> dict:
    """Удаляет событие из Google Calendar пользователя (если было) и чистит ссылку на него."""
    gcal.sync_task_delete(task.get("google_event_id"), user_id, task.get("gcal_calendar_id"))
    if task.get("google_event_id"):
        extra = {"gcal_imported": False, "gcal_calendar_id": None} if task.get("gcal_imported") else None
        _set_event_id(task, user_id, None, extra)
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
    _normalize_recurrence(data, data.get("deadline"))
    _normalize_ends(data)
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать задачу")
    task = result.data[0]
    return _sync_google_upsert(task, user_id)


def _parsed_tasks(raw_text: str, user_id: str, source_hint: Optional[str] = None,
                  default_date: Optional[str] = None) -> list[TaskCreate]:
    """Текст → проверенные задачи (без сохранения). default_date — см. create_tasks_from_text."""
    try:
        default_date = date.fromisoformat(default_date).isoformat() if default_date else None
    except ValueError:
        default_date = None
    names = categories_service.names(user_id) or [categories_service.fallback_name(user_id)]
    extra = {"default_date": default_date} if default_date else {}
    raw_tasks = parse_text_to_tasks(raw_text, names, source_hint=source_hint,
                                    timezone=users_service.timezone_of(user_id), **extra)
    fallback = names[0]
    created = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        # Модель могла вернуть чужое или выдуманное название — подставляем запасную категорию,
        # парсер не должен падать из-за категории.
        raw = {**raw, "category": categories_service.resolve_name(user_id, str(raw.get("category") or "")) or fallback}
        if default_date and not raw.get("deadline"):
            raw["deadline"] = f"{default_date}T23:59:00"  # модель не поставила дату — значит, на открытый день
        try:
            created.append(TaskCreate(**raw))
        except Exception:
            # Пропускаем некорректный элемент от LLM, не роняем остальные
            continue
    return created


def create_tasks_from_text(raw_text: str, user_id: str, source_hint: Optional[str] = None,
                           default_date: Optional[str] = None) -> list[dict]:
    """default_date ("YYYY-MM-DD") — день, открытый в приложении: задачи без даты в тексте встают на него
    (время — из текста или 23:59). Своя дата в тексте главнее."""
    return [create_task(t, user_id) for t in _parsed_tasks(raw_text, user_id, source_hint, default_date)]


def preview_tasks_from_text(raw_text: str, user_id: str) -> list[dict]:
    """Задачи из текста (заметки, /split) БЕЗ сохранения — человек сам отмечает, какие добавить."""
    return [t.model_dump(mode="json") for t in _parsed_tasks(raw_text, user_id)]


def breakdown(task_id: str, user_id: str) -> Optional[list[dict]]:
    """«Разбей на шаги»: ИИ предлагает 3–7 шагов по названию и заметке, сразу добавляем как подзадачи
    (без повторов уже существующих). None — задачи нет или она чужая."""
    task = get_task(task_id, user_id)
    if not task:
        return None
    import groq_client
    import subtasks_service
    from models import SubtaskCreate
    existing = supabase.table("subtasks").select("content").eq("task_id", task_id).execute().data
    have = {(r.get("content") or "").strip().lower() for r in existing}
    steps = groq_client.suggest_steps(task["task"], task.get("note"), [r["content"] for r in existing])
    added = []
    for step in steps:
        key = step.strip().lower()
        if not key or key in have:
            continue
        have.add(key)
        row = subtasks_service.create_subtask(task_id, SubtaskCreate(content=step.strip()[:200]), user_id)
        if row:
            added.append(row)
    return added


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
    if "recurrence_pattern" in update_data or "recurrence_days" in update_data:
        if "recurrence_pattern" not in update_data:
            update_data["recurrence_pattern"] = current.get("recurrence_pattern")
        _normalize_recurrence(update_data, update_data.get("deadline", current.get("deadline")))
    _normalize_ends(update_data, current)
    update_data["updated_at"] = datetime.utcnow().isoformat()

    result = supabase.table(TABLE).update(update_data).eq("id", task_id).eq("user_id", user_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить задачу")
    updated = result.data[0]

    series = gcal.series_of(current.get("google_event_id")) if current.get("gcal_imported") else None
    if series:
        shared = {k: update_data[k] for k in SERIES_FIELDS if k in update_data and update_data[k] != current.get(k)}
        if shared:
            _update_series(user_id, series, task_id, shared)
            gcal.patch_series(user_id, current.get("gcal_calendar_id"), series, shared)

    # Повторяющиеся задачи: закрыли — создаём следующую у того же пользователя
    became_done = update_data.get("status") == Status.done.value
    if became_done and current.get("is_recurring"):
        next_deadline = _next_deadline(
            current.get("deadline") and datetime.fromisoformat(current["deadline"]),
            current.get("recurrence_pattern"), current.get("recurrence_days"), user_id,
        )
        new_task = {
            "user_id": user_id,
            "task": current["task"],
            "category": current["category"],
            "source": current.get("source"),
            "deadline": next_deadline.isoformat() if next_deadline else None,
            "ends_at": (next_deadline + (_dt(current["ends_at"]) - _dt(current["deadline"]))).isoformat()
            if next_deadline and current.get("ends_at") and current.get("deadline") else None,
            "priority": current["priority"],
            "status": Status.todo.value,
            "note": current.get("note"),
            "is_recurring": True,
            "recurrence_pattern": current.get("recurrence_pattern"),
            "recurrence_days": current.get("recurrence_days"),
            "tags": current.get("tags") or [],
        }
        inserted = supabase.table(TABLE).insert(new_task).execute()
        if inserted.data:
            _sync_google_upsert(inserted.data[0], user_id)

    # Синхронизация с Google Calendar: закрыли задачу — убираем событие из
    # календаря; иначе, если есть дедлайн, создаём/обновляем событие;
    # если дедлайн убрали — тоже убираем событие.
    if became_done and updated.get("gcal_imported"):
        pass  # событие создано в Google самим человеком (встреча, пара): остаётся в календаре, ссылка тоже
    elif became_done:
        updated = _sync_google_clear(updated, user_id)
    elif updated.get("deadline"):
        updated = _sync_google_upsert(updated, user_id)
    elif updated.get("google_event_id"):
        updated = _sync_google_clear(updated, user_id)

    return updated


def series_tasks(user_id: str, series_id: str) -> list[dict]:
    """Задачи-повторения одной серии Google Calendar у пользователя."""
    rows = supabase.table(TABLE).select("*").eq("user_id", user_id).eq("gcal_imported", True).execute().data
    return [r for r in rows if gcal.series_of(r.get("google_event_id")) == series_id]


def _update_series(user_id: str, series_id: str, task_id: str, shared: dict) -> None:
    """Остальным незакрытым повторениям серии — те же название, категория, приоритет, заметка."""
    data = {**shared, "updated_at": datetime.utcnow().isoformat()}
    for row in series_tasks(user_id, series_id):
        if row["id"] != task_id and row.get("status") != Status.done.value:
            supabase.table(TABLE).update(data).eq("id", row["id"]).eq("user_id", user_id).execute()


def delete_task(task_id: str, user_id: str) -> bool:
    task = get_task(task_id, user_id)
    if not task:
        return False
    gcal.sync_task_delete(task.get("google_event_id"), user_id, task.get("gcal_calendar_id"))
    import comments_service  # файлы комментариев — из хранилища; строки уйдут каскадом вместе с задачей
    comments_service.delete_task_files(task_id, user_id)
    result = supabase.table(TABLE).delete().eq("id", task_id).eq("user_id", user_id).execute()
    return bool(result.data)
