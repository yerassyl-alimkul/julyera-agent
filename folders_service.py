"""
Папки задач (миграция 015, решение владельца 14 октября 2026): «Поездка в Вену» с задачами внутри.

Папка либо личная (task_folders.user_id — видит только владелец, страница «Задачи → Папки»), либо папки группы
(task_folders.group_id — видят, создают и наполняют все участники группы). Задача в папке — tasks.folder_id;
у общей задачи группы папка одинаковая у всех копий (shared_service.SHARED_FIELDS). Задача в папке группы —
всегда задача этой группы. Удалили папку — задачи остаются без папки. Всё по user_id; в логи — только id.
"""
import logging
import uuid
from typing import Optional

import categories_service
from categories_service import CategoryError
from database import supabase

logger = logging.getLogger(__name__)

TABLE = "task_folders"
NAME_MAX = 40


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _clean_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= NAME_MAX:
        raise CategoryError(422, f"Название папки — от 1 до {NAME_MAX} символов", "bad_name")
    return name


def _member(group_id: str, user_id: str) -> Optional[dict]:
    import groups_service
    return groups_service.group_for_member(str(group_id), user_id)


def get_folder(folder_id, user_id: str) -> Optional[dict]:
    """Папка, если она пользователя или его группы (иначе None — для него её «нет»)."""
    if not folder_id or not _is_uuid(folder_id):
        return None
    rows = supabase.table(TABLE).select("*").eq("id", str(folder_id)).execute().data
    if not rows:
        return None
    f = rows[0]
    if f.get("user_id"):
        return f if f["user_id"] == user_id else None
    return f if _member(f["group_id"], user_id) else None


def _out(f: dict, user_id: str) -> dict:
    tasks = supabase.table("tasks").select("status").eq("user_id", user_id).eq("folder_id", f["id"]).execute().data
    return {"id": f["id"], "name": f["name"], "color": f.get("color"), "group_id": f.get("group_id"),
            "total": len(tasks), "done": sum(1 for t in tasks if t.get("status") == "Done"),
            "can_delete": bool(f.get("user_id")) or f.get("created_by") == user_id or _is_group_owner(f, user_id)}


def _is_group_owner(f: dict, user_id: str) -> bool:
    g = _member(f["group_id"], user_id) if f.get("group_id") else None
    return bool(g and g["owner_id"] == user_id)


def list_folders(user_id: str, group_id: Optional[str] = None) -> list[dict]:
    if group_id:
        if not _member(group_id, user_id):
            raise CategoryError(404, "Группа не найдена", "no_group")
        rows = supabase.table(TABLE).select("*").eq("group_id", str(group_id)).order("created_at").execute().data
    else:
        rows = supabase.table(TABLE).select("*").eq("user_id", user_id).order("created_at").execute().data
    return [_out(f, user_id) for f in rows]


def create_folder(user_id: str, name, group_id: Optional[str] = None) -> dict:
    name = _clean_name(name)
    existing = list_folders(user_id, group_id)
    if any(f["name"].lower() == name.lower() for f in existing):
        raise CategoryError(409, "Папка с таким названием уже есть", "duplicate")
    palette = categories_service.PALETTE
    row = {"name": name, "color": palette[len(existing) % len(palette)], "created_by": user_id}
    if group_id:
        row["group_id"] = str(group_id)
    else:
        row["user_id"] = user_id
    f = supabase.table(TABLE).insert(row).execute().data[0]
    logger.info("Папка %s создана", f["id"])
    return _out(f, user_id)


def rename_folder(folder_id: str, user_id: str, name) -> Optional[dict]:
    f = get_folder(folder_id, user_id)
    if not f:
        return None
    name = _clean_name(name)
    if any(o["id"] != f["id"] and o["name"].lower() == name.lower() for o in list_folders(user_id, f.get("group_id"))):
        raise CategoryError(409, "Папка с таким названием уже есть", "duplicate")
    supabase.table(TABLE).update({"name": name}).eq("id", f["id"]).execute()
    return _out({**f, "name": name}, user_id)


def delete_folder(folder_id: str, user_id: str) -> bool:
    """Личную — владелец; папку группы — тот, кто её создал, или создатель группы. Задачи остаются без папки."""
    f = get_folder(folder_id, user_id)
    if not f:
        return False
    if f.get("group_id") and f.get("created_by") != user_id and not _is_group_owner(f, user_id):
        raise CategoryError(403, "Удалить папку может тот, кто её создал, или создатель группы", "not_owner")
    supabase.table(TABLE).delete().eq("id", f["id"]).execute()
    return True


def check_for_task(folder_id, user_id: str, group_id) -> Optional[dict]:
    """Папка для задачи: своя личная (у обычной задачи) или папка той же группы. Иначе 404/422."""
    if not folder_id:
        return None
    f = get_folder(folder_id, user_id)
    if not f:
        raise CategoryError(404, "Папка не найдена", "no_folder")
    if f.get("group_id") and str(f["group_id"]) != str(group_id or ""):
        raise CategoryError(422, "Эта папка — другой группы", "other_group")
    if f.get("user_id") and group_id:
        raise CategoryError(422, "Задачу группы можно положить только в папку группы", "personal_folder")
    return f
