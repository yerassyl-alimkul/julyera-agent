"""
Группы с общими задачами (миграция 014, решения владельца 11 октября 2026).

У каждой группы своя страница в приложении: задачи создаются там, без выбора «С кем», и сразу появляются у всех
участников (своя копия у каждого — shared_service). Вступают по ссылке t.me/<бот>?start=g_<invite_token>: бот
спрашивает «Вступить в группу?», после «Вступить» человеку приходят и открытые задачи группы. Видеть группу и
добавлять задачи могут все участники; название, режим галочки и состав меняет только создатель, выйти может любой.
tick_all — галочка закрывает задачу у всех или у каждого своя («1 из 3 готово»), выбирается при создании.
У каждого участника задачи группы лежат в категории с её названием. Всё по user_id; в логи — только id.
"""
import logging
import os
import secrets
import uuid
from typing import Optional

import httpx

import users_service
from categories_service import CategoryError
from database import supabase

logger = logging.getLogger(__name__)

GROUPS = "task_groups"
MEMBERS = "task_group_members"
NAME_MAX = 30
_bot_username: list = []


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


def _clean_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= NAME_MAX:
        raise CategoryError(422, f"Название группы — от 1 до {NAME_MAX} символов", "bad_name")
    return name


def member_ids(group_id: str) -> list[str]:
    return [r["user_id"] for r in supabase.table(MEMBERS).select("user_id").eq("group_id", group_id).execute().data]


def _row(group_id: str) -> Optional[dict]:
    if not _is_uuid(group_id):
        return None
    rows = supabase.table(GROUPS).select("*").eq("id", group_id).execute().data
    return rows[0] if rows else None


def group_for_member(group_id: str, user_id: str) -> Optional[dict]:
    """Группа, если пользователь в ней состоит (иначе None — для него её «нет»)."""
    row = _row(group_id)
    return row if row and user_id in member_ids(group_id) else None


def _link(row: dict) -> dict:
    name = bot_username()
    return {"start": f"/start g_{row['invite_token']}",
            "url": f"https://t.me/{name}?start=g_{row['invite_token']}" if name else None}


def _out(row: dict, user_id: str) -> dict:
    members = sorted(({"id": m, "name": _name(m), "owner": m == row["owner_id"]} for m in member_ids(row["id"])),
                     key=lambda m: (not m["owner"], m["name"].lower()))
    return {"id": row["id"], "name": row["name"], "tick_all": bool(row.get("tick_all", True)),
            "owner": row["owner_id"] == user_id, "members": members, "invite": _link(row)}


def list_groups(user_id: str) -> list[dict]:
    ids = [r["group_id"] for r in supabase.table(MEMBERS).select("group_id").eq("user_id", user_id).execute().data]
    if not ids:
        return []
    rows = supabase.table(GROUPS).select("*").in_("id", ids).order("created_at").execute().data
    return [_out(r, user_id) for r in rows]


def get_group(group_id: str, user_id: str) -> Optional[dict]:
    row = group_for_member(group_id, user_id)
    return _out(row, user_id) if row else None


def create_group(user_id: str, name, tick_all: bool = True) -> dict:
    import shared_service
    name = _clean_name(name)
    row = supabase.table(GROUPS).insert({"owner_id": user_id, "name": name, "tick_all": bool(tick_all),
                                         "invite_token": secrets.token_hex(8)}).execute().data[0]
    supabase.table(MEMBERS).insert({"group_id": row["id"], "user_id": user_id}).execute()
    shared_service.ensure_category(user_id, name)
    logger.info("Группа %s создана", row["id"])
    return _out(row, user_id)


def _owned(group_id: str, user_id: str) -> dict:
    row = group_for_member(group_id, user_id)
    if not row:
        raise CategoryError(404, "Группа не найдена", "not_found")
    if row["owner_id"] != user_id:
        raise CategoryError(403, "Это может только создатель группы", "not_owner")
    return row


def update_group(group_id: str, user_id: str, name=None, tick_all=None) -> dict:
    """Переименование (категории участников переименовываются тоже) и режим галочки — только создатель."""
    import categories_service
    row = _owned(group_id, user_id)
    changes = {}
    if name is not None and _clean_name(name) != row["name"]:
        changes["name"] = _clean_name(name)
        for m in member_ids(group_id):
            cat = categories_service._find_by_name(m, row["name"])
            if cat and not categories_service._find_by_name(m, changes["name"]):
                try:
                    categories_service.update_category(cat["id"], m, name=changes["name"])
                except Exception as exc:
                    logger.warning("Категория группы не переименована: %s", type(exc).__name__)
    if tick_all is not None:
        changes["tick_all"] = bool(tick_all)
    if changes:
        supabase.table(GROUPS).update(changes).eq("id", group_id).eq("owner_id", user_id).execute()
    return get_group(group_id, user_id)


def delete_group(group_id: str, user_id: str) -> None:
    """Удалить группу (создатель). Задачи остаются у всех обычными (group_id станет пустым), связь копий тоже."""
    _owned(group_id, user_id)
    others = [m for m in member_ids(group_id) if m != user_id]
    name = _row(group_id)["name"]
    supabase.table(GROUPS).delete().eq("id", group_id).eq("owner_id", user_id).execute()
    import notifier
    for m in others:
        notifier.notify_user(m, f"Группу «{name}» удалили. Её задачи остались у вас.")


def preview(token: str, user_id: str) -> tuple[Optional[dict], str]:
    """Человек открыл ссылку на группу: (группа | None, текст для бота)."""
    rows = supabase.table(GROUPS).select("*").eq("invite_token", (token or "")[:32]).execute().data
    if not rows:
        return None, "Ссылка устарела или группу удалили."
    row = rows[0]
    if user_id in member_ids(row["id"]):
        return None, f"Вы уже в группе «{row['name']}» — она в приложении на вкладке «Группы»."
    return row, f"👥 {_name(row['owner_id'])} зовёт вас в группу «{row['name']}» — общие задачи будут на её странице."


def join(token: str, user_id: str) -> str:
    import notifier
    import shared_service
    row, text = preview(token, user_id)
    if not row:
        return text
    try:
        supabase.table(MEMBERS).insert({"group_id": row["id"], "user_id": user_id}).execute()
    except Exception as exc:  # нажали дважды
        if "duplicate" not in str(exc) and getattr(exc, "code", None) != "23505":
            raise
        return f"Вы уже в группе «{row['name']}»."
    shared_service.ensure_category(user_id, row["name"])
    added = shared_service.copy_open_tasks(row, user_id)
    for m in member_ids(row["id"]):
        if m != user_id:
            notifier.notify_user(m, f"👥 {_name(user_id)} теперь в группе «{row['name']}».")
    logger.info("В группу %s вступил %s", row["id"], user_id)
    tail = f" Добавил открытые задачи группы: {added}." if added else ""
    return f"Готово: вы в группе «{row['name']}». Она в приложении на вкладке «Группы».{tail}"


def leave(group_id: str, user_id: str, by_owner: Optional[str] = None) -> None:
    """Выйти из группы (или убрать участника — создатель): его открытые копии задач группы удаляются (выход из
    задачи), закрытые остаются у него обычными. Создатель выйти не может — только удалить группу."""
    import notifier
    import task_service
    row = group_for_member(group_id, user_id)
    if not row:
        raise CategoryError(404, "Группа не найдена", "not_found")
    if row["owner_id"] == user_id:
        raise CategoryError(422, "Создатель не может выйти — удалите группу", "owner")
    tasks = supabase.table("tasks").select("*").eq("user_id", user_id).eq("group_id", group_id).execute().data
    for t in tasks:
        if t.get("status") == "Done":
            supabase.table("tasks").update({"group_id": None}).eq("id", t["id"]).eq("user_id", user_id).execute()
        else:
            task_service.delete_task(t["id"], user_id, quiet=True)
    supabase.table(MEMBERS).delete().eq("group_id", group_id).eq("user_id", user_id).execute()
    for m in member_ids(group_id):
        notifier.notify_user(m, f"{_name(user_id)} больше не в группе «{row['name']}».")
    if by_owner:
        notifier.notify_user(user_id, f"Вас убрали из группы «{row['name']}». Выполненные задачи остались у вас.")


def remove_member(group_id: str, owner_id: str, member_id: str) -> None:
    _owned(group_id, owner_id)
    if member_id == owner_id:
        raise CategoryError(422, "Себя убрать нельзя — удалите группу", "owner")
    leave(group_id, member_id, by_owner=owner_id)
