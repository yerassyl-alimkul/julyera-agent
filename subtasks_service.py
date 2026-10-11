"""
CRUD-логика для подзадач — чек-листа шагов внутри задачи.

У подзадач нет своей колонки user_id: они принадлежат задаче через task_id.
Поэтому перед любой операцией проверяем, что задача принадлежит пользователю;
чужая подзадача для него «не существует» (маршруты отвечают 404).
У совместной задачи (migration 014) шаги общие: они лежат на главной копии (tasks.shared_id), а наружу
отдаются с task_id копии пользователя.
"""
from database import supabase
from models import SubtaskCreate, SubtaskUpdate

TABLE = "subtasks"


def _own_task(task_id: str, user_id: str) -> dict | None:
    result = supabase.table("tasks").select("id,shared_id").eq("id", task_id).eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def _canonical(task: dict) -> str:
    return task.get("shared_id") or task["id"]


def _copy_for(canonical: str, user_id: str) -> str | None:
    """id копии пользователя, чьи шаги лежат на задаче canonical (или None — не его)."""
    rows = supabase.table("tasks").select("id").eq("id", canonical).eq("user_id", user_id).execute().data
    if not rows:
        rows = supabase.table("tasks").select("id").eq("shared_id", canonical).eq("user_id", user_id).execute().data
    return rows[0]["id"] if rows else None


def create_subtask(task_id: str, payload: SubtaskCreate, user_id: str) -> dict | None:
    task = _own_task(task_id, user_id)
    if not task:
        return None
    data = {"task_id": _canonical(task), "content": payload.content}
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать подзадачу")
    return {**result.data[0], "task_id": task_id}


def list_all_subtasks(user_id: str) -> list[dict]:
    """Все подзадачи задач пользователя разом — для группировки на клиенте по task_id
    (личный проект небольшого масштаба, отдельный запрос на каждую задачу был бы избыточен)."""
    tasks = supabase.table("tasks").select("id,shared_id").eq("user_id", user_id).execute().data
    if not tasks:
        return []
    own = {_canonical(t): t["id"] for t in tasks}
    ids, rows = list(own), []
    for i in range(0, len(ids), 200):
        rows += supabase.table(TABLE).select("*").in_("task_id", ids[i:i + 200]).order("created_at", desc=False).execute().data
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return [{**r, "task_id": own[r["task_id"]]} for r in rows]


def get_subtask(subtask_id: str, user_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", subtask_id).execute()
    if not result.data:
        return None
    subtask = result.data[0]
    mine = _copy_for(subtask["task_id"], user_id)
    return {**subtask, "_own_task_id": mine} if mine else None


def update_subtask(subtask_id: str, payload: SubtaskUpdate, user_id: str) -> dict | None:
    current = get_subtask(subtask_id, user_id)
    if not current:
        return None
    own = current.pop("_own_task_id")
    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return {**current, "task_id": own}
    result = supabase.table(TABLE).update(data).eq("id", subtask_id).eq("task_id", current["task_id"]).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить подзадачу")
    return {**result.data[0], "task_id": own}


def delete_subtask(subtask_id: str, user_id: str) -> bool:
    current = get_subtask(subtask_id, user_id)
    if not current:
        return False
    result = supabase.table(TABLE).delete().eq("id", subtask_id).eq("task_id", current["task_id"]).execute()
    return bool(result.data)
