"""
CRUD-логика для подзадач — чек-листа шагов внутри задачи.

У подзадач нет своей колонки user_id: они принадлежат задаче через task_id.
Поэтому перед любой операцией проверяем, что задача принадлежит пользователю;
чужая подзадача для него «не существует» (маршруты отвечают 404).
"""
from database import supabase
from models import SubtaskCreate, SubtaskUpdate

TABLE = "subtasks"


def _owns_task(task_id: str, user_id: str) -> bool:
    result = supabase.table("tasks").select("id").eq("id", task_id).eq("user_id", user_id).execute()
    return bool(result.data)


def _user_task_ids(user_id: str) -> list[str]:
    result = supabase.table("tasks").select("id").eq("user_id", user_id).execute()
    return [row["id"] for row in result.data]


def create_subtask(task_id: str, payload: SubtaskCreate, user_id: str) -> dict | None:
    if not _owns_task(task_id, user_id):
        return None
    data = {"task_id": task_id, "content": payload.content}
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать подзадачу")
    return result.data[0]


def list_all_subtasks(user_id: str) -> list[dict]:
    """Все подзадачи задач пользователя разом — для группировки на клиенте по task_id
    (личный проект небольшого масштаба, отдельный запрос на каждую задачу был бы избыточен)."""
    task_ids = _user_task_ids(user_id)
    if not task_ids:
        return []
    result = supabase.table(TABLE).select("*").in_("task_id", task_ids).order("created_at", desc=False).execute()
    return result.data


def get_subtask(subtask_id: str, user_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", subtask_id).execute()
    if not result.data:
        return None
    subtask = result.data[0]
    return subtask if _owns_task(subtask["task_id"], user_id) else None


def update_subtask(subtask_id: str, payload: SubtaskUpdate, user_id: str) -> dict | None:
    current = get_subtask(subtask_id, user_id)
    if not current:
        return None
    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return current
    result = supabase.table(TABLE).update(data).eq("id", subtask_id).eq("task_id", current["task_id"]).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить подзадачу")
    return result.data[0]


def delete_subtask(subtask_id: str, user_id: str) -> bool:
    current = get_subtask(subtask_id, user_id)
    if not current:
        return False
    result = supabase.table(TABLE).delete().eq("id", subtask_id).eq("task_id", current["task_id"]).execute()
    return bool(result.data)
