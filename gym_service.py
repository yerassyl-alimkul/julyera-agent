"""
Брони зала UNIME из бота @unimegym_bot → задачи JulYera (и события Google Calendar).

Бот зала шлёт сюда создание и отмену брони через POST /internal/gym/sync (защита — секретный заголовок
X-Gym-Secret, переменная GYM_SYNC_SECRET). Задача создаётся обычным путём (task_service.create_task),
поэтому событие в Google Calendar появляется так же, как у любой задачи. Задачу брони находим по
tasks.source = 'unime-gym:<lid>:<sid>' у этого пользователя.

Время приходит моментом (ISO, UTC) и переводится в часовой пояс пользователя: дедлайн хранится «голыми часами»
(договорённость 1 в CLAUDE.md).
"""
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import categories_service
import task_service
import users_service
from database import supabase
from models import TaskCreate, TaskUpdate

CATEGORY = "Gym"
CATEGORY_COLOR = "#DCE5FB"  # светло-голубой (UniMe), есть в палитре
SOURCE_PREFIX = "unime-gym:"


def _naive_local(moment: datetime, user_id: str) -> datetime:
    """Момент (UTC или с поясом) → «голые часы» в часовом поясе пользователя."""
    if moment.tzinfo is None:
        from datetime import timezone
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(users_service.timezone_of(user_id))).replace(tzinfo=None)


def _find(user_id: str, source: str) -> Optional[dict]:
    rows = supabase.table("tasks").select("*").eq("user_id", user_id).eq("source", source).execute().data
    return rows[0] if rows else None


def _ensure_category(user_id: str) -> str:
    return categories_service.resolve_name(user_id, CATEGORY) or categories_service.create_category(
        user_id, CATEGORY, CATEGORY_COLOR)["name"]


def sync(telegram_id: int, action: str, ref: str, title: Optional[str] = None, start: Optional[datetime] = None,
         end: Optional[datetime] = None, place: Optional[str] = None) -> str:
    """Возвращает: no_user | blocked | created | updated | removed | skipped."""
    user = users_service.get_by_telegram_id(telegram_id)
    if not user:
        return "no_user"
    if user.get("blocked"):
        return "blocked"
    user_id = user["id"]
    source = SOURCE_PREFIX + ref
    existing = _find(user_id, source)

    if action == "remove":
        if existing:
            task_service.delete_task(existing["id"], user_id, quiet=True)
            return "removed"
        return "skipped"

    if not (title and start and end):
        return "skipped"
    deadline, ends_at = _naive_local(start, user_id), _naive_local(end, user_id)
    if existing:
        task_service.update_task(existing["id"], TaskUpdate(task=title, deadline=deadline, ends_at=ends_at,
                                                            location=place), user_id)
        return "updated"
    task_service.create_task(TaskCreate(task=title, category=_ensure_category(user_id), source=source,
                                        deadline=deadline, ends_at=ends_at, location=place), user_id)
    return "created"
