"""
Пользователи сервиса (владелец и допущенные им люди) и привязка Telegram-чатов к ним.

Backend работает с ключом service_role, который обходит защиту в базе, поэтому изоляция данных
держится только на условии user_id в каждом запросе к tasks, notes, subtasks, telegram_chats,
google_credentials. user_id всегда берётся из проверенного входа, клиент его не присылает.
"""
from typing import Optional

from database import supabase

TABLE = "users"
CHATS_TABLE = "telegram_chats"


def get_by_id(user_id: str) -> Optional[dict]:
    result = supabase.table(TABLE).select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else None


def get_by_telegram_id(telegram_user_id: int) -> Optional[dict]:
    result = supabase.table(TABLE).select("*").eq("telegram_user_id", int(telegram_user_id)).execute()
    return result.data[0] if result.data else None


def get_by_access_key(access_key: str) -> Optional[dict]:
    if not access_key:
        return None
    result = supabase.table(TABLE).select("*").eq("access_key", access_key).execute()
    return result.data[0] if result.data else None


def create_user(name: str, telegram_user_id: int) -> dict:
    """Создаёт пользователя. Если человек с таким Telegram-id уже есть, возвращает его (без дубля)."""
    existing = get_by_telegram_id(telegram_user_id)
    if existing:
        return existing
    result = supabase.table(TABLE).insert(
        {"name": name or f"Пользователь {telegram_user_id}", "telegram_user_id": int(telegram_user_id)}
    ).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать пользователя")
    return result.data[0]


def list_users() -> list[dict]:
    """Все пользователи — для рассылки напоминаний."""
    return supabase.table(TABLE).select("*").execute().data


def list_admins() -> list[dict]:
    return [u for u in list_users() if u.get("is_admin")]


def is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("is_admin"))


def link_chat(chat_id: int, user_id: str) -> None:
    """Запоминает чат пользователя, чтобы слать туда напоминания."""
    supabase.table(CHATS_TABLE).upsert({"chat_id": int(chat_id), "user_id": user_id}).execute()


def get_chat_ids(user_id: str) -> list[int]:
    result = supabase.table(CHATS_TABLE).select("chat_id").eq("user_id", user_id).execute()
    return [row["chat_id"] for row in result.data]
