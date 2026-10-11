"""
Простая CRUD-логика для заметок — свободного текста, не привязанного к задачам.
Каждый запрос фильтруется по user_id владельца заметок.

Список с галочками хранится прямо в тексте строками «- [ ] …» / «- [x] …» (галочку ставит приложение правкой
текста), ссылки — обычный текст, кликабельными их делает интерфейс. Фото (миграция 013) — note_files, файлы
в закрытом бакете task-files: <user_id>/notes/<note_id>/<id>.<ext>, наружу — подписанные ссылки на час.
"""
import logging
import uuid
from datetime import datetime

import comments_service
from database import supabase
from models import NoteCreate, NoteUpdate

logger = logging.getLogger(__name__)

TABLE = "notes"
FILES = "note_files"
PHOTO_TYPES = {k: v for k, v in comments_service.FILE_TYPES.items() if k.startswith("image/")}
PHOTOS_MAX = 10


def _files_by_note(user_id: str) -> dict:
    try:
        rows = supabase.table(FILES).select("*").eq("user_id", user_id).order("created_at").execute().data
    except Exception as exc:  # до миграции 013 таблицы нет — заметки показываются без фото
        logger.warning("Фото заметок недоступны: %s", type(exc).__name__)
        return {}
    urls = comments_service._signed_urls([r["file_path"] for r in rows])
    out = {}
    for r in rows:
        out.setdefault(r["note_id"], []).append({"id": r["id"], "type": r["file_type"], "url": urls.get(r["file_path"])})
    return out


def create_note(payload: NoteCreate, user_id: str) -> dict:
    data = payload.model_dump(mode="json", exclude_none=True)  # место пишем, только если оно есть
    data["user_id"] = user_id  # владельца ставит сервер, не клиент
    result = supabase.table(TABLE).insert(data).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать заметку")
    return result.data[0]


def list_notes(user_id: str) -> list[dict]:
    result = supabase.table(TABLE).select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    files = _files_by_note(user_id) if result.data else {}
    return [{**n, "files": files.get(n["id"], [])} for n in result.data]


def get_note(note_id: str, user_id: str) -> dict | None:
    result = supabase.table(TABLE).select("*").eq("id", note_id).eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def update_note(note_id: str, payload: NoteUpdate, user_id: str) -> dict | None:
    current = get_note(note_id, user_id)
    if not current:
        return None
    data = payload.model_dump(mode="json", exclude_unset=True)
    if "content" in data and data["content"] is None:
        data.pop("content")
    if "location" in data:
        data["location"] = (data["location"] or "").strip() or None
    data["updated_at"] = datetime.utcnow().isoformat()
    result = supabase.table(TABLE).update(data).eq("id", note_id).eq("user_id", user_id).execute()
    if not result.data:
        raise RuntimeError("Не удалось обновить заметку")
    return result.data[0]


def delete_note(note_id: str, user_id: str) -> bool:
    try:  # строки фото уйдут каскадом, файлы из хранилища убираем сами
        paths = [r["file_path"] for r in supabase.table(FILES).select("file_path").eq("note_id", note_id)
                 .eq("user_id", user_id).execute().data]
    except Exception:
        paths = []
    result = supabase.table(TABLE).delete().eq("id", note_id).eq("user_id", user_id).execute()
    if result.data:
        comments_service._remove_files(paths)
    return bool(result.data)


def add_photo(note_id: str, user_id: str, data: bytes, file_type: str) -> dict | None:
    """Фото к своей заметке. None — заметки нет или она чужая."""
    if not get_note(note_id, user_id):
        return None
    if file_type not in PHOTO_TYPES:
        raise comments_service.CommentError(422, "К заметке можно прикрепить только фото", "bad_file_type")
    count = len(supabase.table(FILES).select("id").eq("note_id", note_id).eq("user_id", user_id).execute().data)
    if count >= PHOTOS_MAX:
        raise comments_service.CommentError(422, f"В заметке не больше {PHOTOS_MAX} фото", "too_many")
    file_id = str(uuid.uuid4())
    path = f"{user_id}/notes/{note_id}/{file_id}.{PHOTO_TYPES[file_type]}"
    supabase.storage.from_(comments_service.BUCKET).upload(path, data, {"content-type": file_type})
    try:
        row = supabase.table(FILES).insert({"id": file_id, "note_id": note_id, "user_id": user_id, "file_path": path,
                                            "file_type": file_type, "file_size": len(data)}).execute().data[0]
    except Exception:
        comments_service._remove_files([path])
        raise
    logger.info("Фото %s к заметке %s", file_id, note_id)
    return {"id": row["id"], "type": file_type, "url": comments_service._signed_urls([path]).get(path)}


def delete_photo(file_id: str, user_id: str) -> bool:
    rows = supabase.table(FILES).select("*").eq("id", file_id).eq("user_id", user_id).execute().data
    if not rows:
        return False
    supabase.table(FILES).delete().eq("id", file_id).eq("user_id", user_id).execute()
    comments_service._remove_files([rows[0]["file_path"]])
    return True
