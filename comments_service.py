"""
Комментарии к задачам: текст (ссылки становятся кликабельными в интерфейсе), фото и PDF.

Файлы лежат в закрытом хранилище Supabase Storage (бакет task-files, миграция 005): путь
<user_id>/<task_id>/<comment_id>.<ext>. Наружу отдаются только подписанные ссылки, живущие час —
<img> в приложении не может послать заголовок входа, поэтому прокси через backend не подходит.
Изоляция: комментарий всегда проверяется через задачу пользователя (task_service.get_task) и по user_id.
В логи не пишем ни текст комментария, ни имя файла.
"""
import base64
import binascii
import logging
import uuid
from typing import Optional

import task_service
from database import supabase

logger = logging.getLogger(__name__)

TABLE = "task_comments"
BUCKET = "task-files"
TEXT_MAX = 2000
FILE_MAX = 10 * 1024 * 1024  # 10 МБ, как лимит бакета
URL_TTL = 3600
FILE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic",
              "image/heif": "heif", "application/pdf": "pdf"}


class CommentError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _signed_urls(paths: list[str]) -> dict:
    if not paths:
        return {}
    try:
        items = supabase.storage.from_(BUCKET).create_signed_urls(paths, URL_TTL)
    except Exception as exc:
        logger.warning("Ссылки на файлы не получены: %s", type(exc).__name__)
        return {}
    out = {}
    for item in items or []:
        url = item.get("signedURL") or item.get("signedUrl")
        if item.get("path") and url:
            out[item["path"]] = url
    return out


def _out(row: dict, urls: dict) -> dict:
    return {"id": row["id"], "task_id": row["task_id"], "text": row.get("text"), "created_at": row.get("created_at"),
            "file": ({"name": row.get("file_name"), "type": row.get("file_type"), "size": row.get("file_size"),
                      "url": urls.get(row["file_path"])} if row.get("file_path") else None)}


def list_comments(task_id: str, user_id: str) -> Optional[list[dict]]:
    """Комментарии своей задачи, старые сверху. None — задачи нет или она чужая."""
    if not task_service.get_task(task_id, user_id):
        return None
    rows = supabase.table(TABLE).select("*").eq("task_id", task_id).eq("user_id", user_id).order("created_at").execute().data
    urls = _signed_urls([r["file_path"] for r in rows if r.get("file_path")])
    return [_out(r, urls) for r in rows]


def decode_file(data_base64: Optional[str], file_type: Optional[str]) -> tuple[bytes, str]:
    """Файл из JSON (base64) → (байты, тип). Только фото и PDF, не больше 10 МБ."""
    file_type = (file_type or "").lower()
    if file_type not in FILE_TYPES:
        raise CommentError(422, "Можно прикрепить фото (JPEG, PNG, WebP, HEIC) или PDF", "bad_file_type")
    try:
        data = base64.b64decode(data_base64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise CommentError(422, "Файл повреждён, попробуйте ещё раз", "bad_file")
    if not data:
        raise CommentError(422, "Файл пустой", "bad_file")
    if len(data) > FILE_MAX:
        raise CommentError(422, "Файл больше 10 МБ", "file_too_big")
    return data, file_type


def add_comment(task_id: str, user_id: str, text: Optional[str] = None, file_bytes: Optional[bytes] = None,
                file_type: Optional[str] = None, file_name: Optional[str] = None) -> Optional[dict]:
    """Добавить комментарий (текст и/или файл) к своей задаче. None — задачи нет или она чужая."""
    if not task_service.get_task(task_id, user_id):
        return None
    text = (text or "").strip() or None
    if text and len(text) > TEXT_MAX:
        raise CommentError(422, f"Комментарий — не длиннее {TEXT_MAX} символов", "bad_text")
    if not text and not file_bytes:
        raise CommentError(422, "Напишите текст или прикрепите файл", "empty")
    comment_id = str(uuid.uuid4())
    row = {"id": comment_id, "task_id": task_id, "user_id": user_id, "text": text}
    if file_bytes:
        if file_type not in FILE_TYPES:
            raise CommentError(422, "Можно прикрепить фото (JPEG, PNG, WebP, HEIC) или PDF", "bad_file_type")
        if len(file_bytes) > FILE_MAX:
            raise CommentError(422, "Файл больше 10 МБ", "file_too_big")
        path = f"{user_id}/{task_id}/{comment_id}.{FILE_TYPES[file_type]}"
        supabase.storage.from_(BUCKET).upload(path, file_bytes, {"content-type": file_type})
        name = (file_name or "").strip()[:120] or f"файл.{FILE_TYPES[file_type]}"
        row.update(file_path=path, file_name=name, file_type=file_type, file_size=len(file_bytes))
    try:
        rows = supabase.table(TABLE).insert(row).execute().data
    except Exception:
        if row.get("file_path"):  # файл без комментария не оставляем
            _remove_files([row["file_path"]])
        raise
    logger.info("Комментарий %s к задаче %s, файл: %s", comment_id, task_id, bool(row.get("file_path")))
    return _out(rows[0], _signed_urls([row["file_path"]]) if row.get("file_path") else {})


def _remove_files(paths: list[str]) -> None:
    if not paths:
        return
    try:
        supabase.storage.from_(BUCKET).remove(paths)
    except Exception as exc:
        logger.warning("Файлы комментариев не удалены из хранилища: %s", type(exc).__name__)


def delete_comment(comment_id: str, user_id: str) -> bool:
    rows = supabase.table(TABLE).select("*").eq("id", comment_id).eq("user_id", user_id).execute().data
    if not rows:
        return False
    supabase.table(TABLE).delete().eq("id", comment_id).eq("user_id", user_id).execute()
    if rows[0].get("file_path"):
        _remove_files([rows[0]["file_path"]])
    return True


def delete_task_files(task_id: str, user_id: str) -> None:
    """Перед удалением задачи: строки комментариев уйдут каскадом, а файлы из хранилища удаляем сами."""
    try:
        rows = supabase.table(TABLE).select("file_path").eq("task_id", task_id).eq("user_id", user_id).execute().data
        _remove_files([r["file_path"] for r in rows if r.get("file_path")])
    except Exception as exc:
        logger.warning("Файлы задачи не удалены: %s", type(exc).__name__)


def counts(user_id: str) -> dict:
    """{task_id: {"comments": n, "files": m}} — для значков 💬 и 📎 в списке задач."""
    try:
        rows = supabase.table(TABLE).select("task_id,file_path").eq("user_id", user_id).execute().data
    except Exception as exc:  # до миграции 005 таблицы нет — список задач от этого не ломается
        logger.warning("Счётчики комментариев недоступны: %s", type(exc).__name__)
        return {}
    out = {}
    for r in rows:
        c = out.setdefault(r["task_id"], {"comments": 0, "files": 0})
        c["comments"] += 1
        if r.get("file_path"):
            c["files"] += 1
    return out
