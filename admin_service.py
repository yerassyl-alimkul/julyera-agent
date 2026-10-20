"""
Администрирование (решение владельца 20 октября 2026): владелец (users.is_admin) видит список людей и их задачи
и может отключить человеку доступ (users.blocked, миграция 016). Только просмотр задач — менять и удалять чужие
задачи нельзя. Заметки, кошелёк, цели и комментарии сюда не попадают. Отключённый человек не входит ни в
приложение, ни в бота, не получает рассылок; его данные остаются, доступ можно вернуть.
В логи — только id.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import users_service
from categories_service import CategoryError
from database import supabase

logger = logging.getLogger(__name__)


def _tasks(user_id: str) -> list[dict]:
    import money_service
    return money_service._fetch_all(lambda: supabase.table("tasks").select("*").eq("user_id", user_id))


def users_overview(admin_id: str) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for u in users_service.list_users(include_blocked=True):
        tasks = supabase.table("tasks").select("status,deadline,updated_at").eq("user_id", u["id"]).execute().data
        open_ = [t for t in tasks if t.get("status") != "Done"]
        overdue = [t for t in open_ if t.get("deadline") and str(t["deadline"])[:10] < today]
        last = max((str(t.get("updated_at") or "") for t in tasks), default="") or None
        out.append({"id": u["id"], "name": u.get("name") or "Без имени", "is_admin": bool(u.get("is_admin")),
                    "blocked": bool(u.get("blocked")), "me": u["id"] == admin_id,
                    "open": len(open_), "done": len(tasks) - len(open_), "overdue": len(overdue), "last_activity": last})
    return sorted(out, key=lambda x: (not x["me"], x["blocked"], x["name"].lower()))


def user_tasks(user_id: str) -> Optional[dict]:
    user = users_service.get_by_id(user_id)
    if not user:
        return None
    tasks = _tasks(user_id)
    canon = {t.get("shared_id") or t["id"]: t["id"] for t in tasks}
    steps = []
    ids = list(canon)
    for i in range(0, len(ids), 200):
        steps += supabase.table("subtasks").select("*").in_("task_id", ids[i:i + 200]).execute().data
    fields = ("id", "task", "category", "deadline", "ends_at", "status", "note", "location", "is_recurring",
              "recurrence_pattern", "group_id", "folder_id", "updated_at")
    return {"user": {"id": user["id"], "name": user.get("name"), "blocked": bool(user.get("blocked"))},
            "tasks": [{k: t.get(k) for k in fields} for t in tasks],
            "subtasks": [{"id": s["id"], "task_id": canon[s["task_id"]], "content": s["content"], "is_done": s.get("is_done")}
                         for s in steps]}


def set_blocked(admin_id: str, user_id: str, blocked: bool) -> dict:
    user = users_service.get_by_id(user_id)
    if not user:
        raise CategoryError(404, "Пользователь не найден", "not_found")
    if user_id == admin_id or user.get("is_admin"):
        raise CategoryError(422, "Себя и владельцев отключить нельзя", "admin")
    users_service.set_blocked(user_id, blocked)
    logger.info("Доступ %s: %s", user_id, "отключён" if blocked else "возвращён")
    return {"id": user_id, "blocked": bool(blocked)}
