"""
Простая CRUD-логика для заметок — свободного текста, не привязанного к задачам.
Каждый запрос фильтруется по user_id владельца заметок.
"""
from datetime import datetime

from database import supabase
from models import NoteCreate, NoteUpdate

TABLE = "notes"


def create_note(payload: NoteCreate, user_id: str) -> dict:
    data = payload.model_dump(mode="json")
    data["user_id"] = user_id  # владельца ставит сервер, не клиент
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать заметку")
    return result.data[0]


def list_notes(user_id: str) -> list[dict]:
    result = supabase.table(TABLE).select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return result.data


def get_note(note_id: str, user_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", note_id).eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def update_note(note_id: str, payload: NoteUpdate, user_id: str) -> dict | None:
    current = get_note(note_id, user_id)
    if not current:
        return None
    data = payload.model_dump(mode="json")
    data["updated_at"] = datetime.utcnow().isoformat()
    result = supabase.table(TABLE).update(data).eq("id", note_id).eq("user_id", user_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить заметку")
    return result.data[0]


def delete_note(note_id: str, user_id: str) -> bool:
    result = supabase.table(TABLE).delete().eq("id", note_id).eq("user_id", user_id).execute()
    return bool(result.data)
