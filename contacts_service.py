"""
Контакты и группы для совместных задач (миграция 014, решение владельца 11 октября 2026).

Контакты — по взаимному согласию: пользователь делится ссылкой t.me/<бот>?start=c_<токен> (contact_links, живёт
7 дней), второй открывает её в боте и нажимает «Принять» (`ct:y:<токен>`) — тогда оба видят друг друга в «С кем».
Без контакта человека не видно и задачу ему не отправить. Группа — личный сохранённый набор своих контактов
(видит и меняет только её автор): задача на двоих и больше людей ставится только группой.
Всё по user_id; в логи — только id.
"""
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import users_service
from categories_service import CategoryError
from database import supabase

logger = logging.getLogger(__name__)

CONTACTS = "contacts"
LINKS = "contact_links"
GROUPS = "task_groups"
MEMBERS = "task_group_members"
LINK_TTL = timedelta(days=7)
GROUP_NAME_MAX = 30
_bot_username: list = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _name(user_id: str) -> str:
    return (users_service.get_by_id(user_id) or {}).get("name") or "Без имени"


def bot_username() -> Optional[str]:
    """Имя бота для ссылки (getMe один раз). Нет токена или сети — None."""
    if _bot_username:
        return _bot_username[0]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        name = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()["result"]["username"]
    except Exception as exc:
        logger.warning("Имя бота не получено: %s", type(exc).__name__)
        return None
    _bot_username.append(name)
    return name


# ---------------------------------------------------------------- контакты

def contacts(user_id: str) -> list[dict]:
    rows = supabase.table(CONTACTS).select("contact_id").eq("user_id", user_id).execute().data
    out = [{"id": r["contact_id"], "name": _name(r["contact_id"])} for r in rows]
    return sorted(out, key=lambda c: c["name"].lower())


def is_contact(user_id: str, other_id: str) -> bool:
    return bool(supabase.table(CONTACTS).select("contact_id").eq("user_id", user_id).eq("contact_id", other_id).execute().data)


def _add_pair(a: str, b: str) -> None:
    for x, y in ((a, b), (b, a)):
        try:
            supabase.table(CONTACTS).insert({"user_id": x, "contact_id": y}).execute()
        except Exception as exc:  # уже в контактах
            if "duplicate" not in str(exc) and getattr(exc, "code", None) != "23505":
                raise


def link(user_id: str) -> dict:
    """Ссылка-приглашение в контакты (одна действующая на пользователя, 7 дней)."""
    rows = supabase.table(LINKS).select("*").eq("user_id", user_id).execute().data
    fresh = [r for r in rows if datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00")) > _now() - LINK_TTL]
    if fresh:
        token = fresh[0]["token"]
    else:
        for r in rows:
            supabase.table(LINKS).delete().eq("token", r["token"]).eq("user_id", user_id).execute()
        token = secrets.token_hex(8)
        supabase.table(LINKS).insert({"token": token, "user_id": user_id}).execute()
    name = bot_username()
    return {"token": token, "start": f"/start c_{token}",
            "url": f"https://t.me/{name}?start=c_{token}" if name else None}


def _link_owner(token: str) -> Optional[str]:
    if not token or len(token) > 32:
        return None
    rows = supabase.table(LINKS).select("*").eq("token", token).execute().data
    if not rows:
        return None
    if datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00")) <= _now() - LINK_TTL:
        return None
    return rows[0]["user_id"]


def preview(token: str, user_id: str) -> tuple[Optional[str], str]:
    """Человек открыл ссылку: (кто приглашает | None, текст для бота)."""
    owner = _link_owner(token)
    if not owner:
        return None, "Ссылка устарела. Попросите прислать новую."
    if owner == user_id:
        return None, "Это ваша ссылка — отправьте её человеку, с которым хотите ставить общие задачи."
    if is_contact(user_id, owner):
        return None, f"{_name(owner)} уже в ваших контактах."
    return owner, f"👋 {_name(owner)} хочет добавить вас в контакты JulYera — чтобы ставить задачи на двоих."


def accept(token: str, user_id: str) -> str:
    owner = _link_owner(token)
    if not owner:
        return "Ссылка устарела. Попросите прислать новую."
    if owner == user_id:
        return "Это ваша собственная ссылка."
    _add_pair(owner, user_id)
    import notifier
    notifier.notify_user(owner, f"👥 {_name(user_id)} теперь в ваших контактах — можно ставить общие задачи.")
    logger.info("Контакты: %s и %s", owner, user_id)
    return f"Готово: {_name(owner)} в ваших контактах."


def remove(user_id: str, contact_id: str) -> bool:
    """Убрать из контактов (у обоих) и из групп обоих. Уже общие задачи остаются."""
    if not _is_uuid(contact_id) or not is_contact(user_id, contact_id):
        return False
    for x, y in ((user_id, contact_id), (contact_id, user_id)):
        supabase.table(CONTACTS).delete().eq("user_id", x).eq("contact_id", y).execute()
        for g in supabase.table(GROUPS).select("id").eq("user_id", x).execute().data:
            supabase.table(MEMBERS).delete().eq("group_id", g["id"]).eq("member_id", y).execute()
    return True


# ---------------------------------------------------------------- группы

def _clean_group_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= GROUP_NAME_MAX:
        raise CategoryError(422, f"Название группы — от 1 до {GROUP_NAME_MAX} символов", "bad_name")
    return name


def _clean_members(user_id: str, ids) -> list[str]:
    ids = list(dict.fromkeys(str(i) for i in (ids or [])))
    mine = {c["id"] for c in contacts(user_id)}
    if any(i not in mine for i in ids):
        raise CategoryError(422, "В группу можно добавить только своих контактов", "not_contact")
    if len(ids) < 2:
        raise CategoryError(422, "В группе нужно хотя бы двое — для одного человека группа не нужна", "too_few")
    return ids


def _group_row(group_id: str, user_id: str) -> Optional[dict]:
    if not _is_uuid(group_id):
        return None
    rows = supabase.table(GROUPS).select("*").eq("id", group_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def members(group_id: str) -> list[str]:
    return [r["member_id"] for r in supabase.table(MEMBERS).select("member_id").eq("group_id", group_id).execute().data]


def _out(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"],
            "members": sorted(({"id": m, "name": _name(m)} for m in members(row["id"])), key=lambda m: m["name"].lower())}


def list_groups(user_id: str) -> list[dict]:
    rows = supabase.table(GROUPS).select("*").eq("user_id", user_id).order("created_at").execute().data
    return [_out(r) for r in rows]


def get_group(group_id: str, user_id: str) -> Optional[dict]:
    row = _group_row(group_id, user_id)
    return _out(row) if row else None


def create_group(user_id: str, name, member_ids) -> dict:
    name = _clean_group_name(name)
    ids = _clean_members(user_id, member_ids)
    if any(g["name"].lower() == name.lower() for g in list_groups(user_id)):
        raise CategoryError(409, "Группа с таким названием уже есть", "duplicate")
    row = supabase.table(GROUPS).insert({"user_id": user_id, "name": name}).execute().data[0]
    for m in ids:
        supabase.table(MEMBERS).insert({"group_id": row["id"], "member_id": m}).execute()
    logger.info("Группа %s: %d человек", row["id"], len(ids))
    return _out(row)


def update_group(group_id: str, user_id: str, name=None, member_ids=None) -> Optional[dict]:
    row = _group_row(group_id, user_id)
    if not row:
        return None
    if name is not None:
        new = _clean_group_name(name)
        if any(g["id"] != group_id and g["name"].lower() == new.lower() for g in list_groups(user_id)):
            raise CategoryError(409, "Группа с таким названием уже есть", "duplicate")
        supabase.table(GROUPS).update({"name": new}).eq("id", group_id).eq("user_id", user_id).execute()
    if member_ids is not None:
        ids = _clean_members(user_id, member_ids)
        supabase.table(MEMBERS).delete().eq("group_id", group_id).execute()
        for m in ids:
            supabase.table(MEMBERS).insert({"group_id": group_id, "member_id": m}).execute()
    return get_group(group_id, user_id)


def delete_group(group_id: str, user_id: str) -> bool:
    if not _group_row(group_id, user_id):
        return False
    supabase.table(GROUPS).delete().eq("id", group_id).eq("user_id", user_id).execute()  # участники — каскадом
    return True
