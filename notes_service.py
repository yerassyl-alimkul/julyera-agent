"""
Простая CRUD-логика для заметок — свободного текста, не привязанного к задачам.
"""
from datetime import datetime

from database import supabase
from models import NoteCreate, NoteUpdate

TABLE = "notes"


def create_note(payload: NoteCreate) -> dict:
    data = payload.model_dump(mode="json")
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать заметку")
    return result.data[0]


def list_notes() -> list[dict]:
    result = supabase.table(TABLE).select("*").order("created_at", desc=True).execute()
    return result.data


def get_note(note_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", note_id).execute()
    return result.data[0] if result.data else None


def update_note(note_id: str, payload: NoteUpdate) -> dict | None:
    current = get_note(note_id)
    if not current:
        return None
    data = payload.model_dump(mode="json")
    data["updated_at"] = datetime.utcnow().isoformat()
    result = supabase.table(TABLE).update(data).eq("id", note_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить заметку")
    return result.data[0]


def delete_note(note_id: str) -> bool:
    result = supabase.table(TABLE).delete().eq("id", note_id).execute()
    return bool(result.data)
