"""
CRUD-логика для подзадач — чек-листа шагов внутри задачи.
"""
from database import supabase
from models import SubtaskCreate, SubtaskUpdate

TABLE = "subtasks"


def create_subtask(task_id: str, payload: SubtaskCreate) -> dict:
    data = {"task_id": task_id, "content": payload.content}
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать подзадачу")
    return result.data[0]


def list_all_subtasks() -> list[dict]:
    """Все подзадачи разом — для группировки на клиенте по task_id
    (личный проект небольшого масштаба, отдельный запрос на каждую
    задачу был бы избыточен)."""
    result = supabase.table(TABLE).select("*").order("created_at", desc=False).execute()
    return result.data


def get_subtask(subtask_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", subtask_id).execute()
    return result.data[0] if result.data else None


def update_subtask(subtask_id: str, payload: SubtaskUpdate) -> dict | None:
    current = get_subtask(subtask_id)
    if not current:
        return None
    data = payload.model_dump(mode="json", exclude_none=True)
    if not data:
        return current
    result = supabase.table(TABLE).update(data).eq("id", subtask_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить подзадачу")
    return result.data[0]


def delete_subtask(subtask_id: str) -> bool:
    result = supabase.table(TABLE).delete().eq("id", subtask_id).execute()
    return bool(result.data)
