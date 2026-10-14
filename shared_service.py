"""
Общие задачи групп (миграция 014, решения владельца 11 октября 2026).

Задача, созданная на странице группы (tasks.group_id), сразу появляется у всех участников: у каждого своя копия,
у всех копий shared_id = id главной копии (на ней общие шаги и комментарии — subtasks_service и comments_service
ищут их по главной копии). У каждого своя категория с названием группы, свой календарь и напоминания.

Название, срок, «до», место, заметка и приоритет меняются у всех копий. Галочка — по режиму группы (tick_all):
закрывает у всех (остальным «✅ … — выполнено (имя)») или у каждого своя (остальным «готово N из M»).
Удаление своей копии — выход из задачи: у остальных она остаётся; осталась одна копия — становится обычной.
Повтор у задачи группы не поддерживается (422). Копию меняет только её владелец. В логи — только id.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import categories_service
import users_service
from categories_service import CategoryError
from database import supabase

logger = logging.getLogger(__name__)

TASKS = "tasks"
SHARED_FIELDS = ("task", "deadline", "ends_at", "note", "location", "priority", "status", "folder_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(user_id: str) -> str:
    return (users_service.get_by_id(user_id) or {}).get("name") or "Без имени"


def ensure_category(user_id: str, name: str) -> str:
    """Категория с таким названием у пользователя (создаётся свободным цветом палитры, если её нет)."""
    name = name.strip()[:30]
    found = categories_service._find_by_name(user_id, name)
    if found:
        return found["name"]
    used = {c["color"] for c in categories_service.list_categories(user_id)}
    color = next((c for c in reversed(categories_service.PALETTE) if c not in used), categories_service.PALETTE[-1])
    try:
        return categories_service.create_category(user_id, name, color)["name"]
    except CategoryError:  # создали параллельно
        return name


def copies(task: dict) -> list[dict]:
    """Все копии общей задачи (включая эту). Обычная задача — только она сама."""
    if not task.get("shared_id"):
        return [task]
    return supabase.table(TASKS).select("*").eq("shared_id", task["shared_id"]).execute().data or [task]


def _group(task: dict) -> Optional[dict]:
    if not task.get("group_id"):
        return None
    rows = supabase.table("task_groups").select("*").eq("id", task["group_id"]).execute().data
    return rows[0] if rows else None


def _tick_all(task: dict) -> bool:
    group = _group(task)
    return bool(group.get("tick_all", True)) if group else True


def prepare_group_task(data: dict, user_id: str) -> Optional[dict]:
    """Перед созданием задачи с group_id: проверка участия, категория — название группы, без повтора."""
    import groups_service
    group_id = data.get("group_id")
    if not group_id:
        data.pop("group_id", None)
        return None
    group = groups_service.group_for_member(str(group_id), user_id)
    if not group:
        raise CategoryError(404, "Группа не найдена", "no_group")
    if data.get("recurrence_pattern") or data.get("is_recurring"):
        raise CategoryError(422, "Задачу группы пока нельзя сделать повторяющейся", "shared_recurring")
    data["group_id"] = str(group_id)
    data["category"] = ensure_category(user_id, group["name"])
    return group


def _copy_row(source: dict, user_id: str, group: dict, canonical: str) -> dict:
    row = {"user_id": user_id, "task": source["task"], "category": ensure_category(user_id, group["name"]),
           "source": "Группа", "deadline": source.get("deadline"), "ends_at": source.get("ends_at"),
           "priority": source.get("priority") or "Mid", "status": "To Do", "note": source.get("note"),
           "is_recurring": False, "tags": [], "shared_id": canonical, "group_id": group["id"]}
    if source.get("folder_id"):  # папка группы — общая
        row["folder_id"] = source["folder_id"]
    if source.get("location"):
        row["location"] = source["location"]
    return row


def spread(task: dict, group: dict, actor_id: str) -> dict:
    """Новая задача группы — копии остальным участникам (без приглашений) и сообщение в бот."""
    import groups_service
    import notifier
    import task_service
    others = [m for m in groups_service.member_ids(group["id"]) if m != actor_id]
    if not others:
        return task
    canonical = task["id"]
    supabase.table(TASKS).update({"shared_id": canonical}).eq("id", task["id"]).eq("user_id", actor_id).execute()
    task = {**task, "shared_id": canonical}
    when = _when(task)
    for m in others:
        copy = supabase.table(TASKS).insert(_copy_row(task, m, group, canonical)).execute().data[0]
        task_service._sync_google_upsert(copy, m)
        notifier.notify_user(m, f"👥 {group['name']}: новая задача от {_name(actor_id)}\n{task['task']}{when}")
    logger.info("Задача группы %s: копий %d", task["id"], len(others))
    return task


def copy_open_tasks(group: dict, user_id: str) -> int:
    """Новый участник получает открытые задачи группы (по одной копии на задачу)."""
    import task_service
    rows = supabase.table(TASKS).select("*").eq("group_id", group["id"]).execute().data
    by_canon = {}
    for r in rows:
        if r.get("user_id") == user_id or r.get("status") == "Done":
            continue
        by_canon.setdefault(r.get("shared_id") or r["id"], r)
    added = 0
    for canonical, source in by_canon.items():
        if not source.get("shared_id"):
            supabase.table(TASKS).update({"shared_id": canonical}).eq("id", source["id"]).execute()
        copy = supabase.table(TASKS).insert(_copy_row(source, user_id, group, canonical)).execute().data[0]
        task_service._sync_google_upsert(copy, user_id)
        added += 1
    return added


def _when(task: dict) -> str:
    if not task.get("deadline"):
        return ""
    d = datetime.fromisoformat(str(task["deadline"]).replace("Z", "+00:00")).replace(tzinfo=None)
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    time = "" if d.strftime("%H:%M") == "23:59" else d.strftime(" · %H:%M")
    return f"\n{days[d.weekday()]}, {d.strftime('%d.%m')}{time}"


def _cmp(field: str, value) -> str:
    """Значение для сравнения: у дат без суффикса пояса (Supabase отдаёт «+00:00», «голые часы»)."""
    text = str(value or "")
    return text[:19] if field in ("deadline", "ends_at") else text


def propagate(task: dict, changes: dict, actor_id: str) -> None:
    """Изменили свою копию — те же общие поля у остальных копий (без повторной рассылки по кругу)."""
    import task_service
    from models import TaskUpdate
    shared = {k: v for k, v in changes.items() if k in SHARED_FIELDS}
    if not shared or not task.get("shared_id"):
        return
    group = copies(task)
    if not _tick_all(task):  # у каждого своя галочка — остальным только «готово N из M»
        status = shared.pop("status", None)
        if status == "Done" and task.get("status") != "Done":
            done = sum(1 for c in group if c.get("status") == "Done" or c["id"] == task["id"])
            import notifier
            for c in group:
                if c["user_id"] != actor_id:
                    notifier.notify_user(c["user_id"], f"✅ «{task['task']}»: {_name(actor_id)} — готово ({done} из {len(group)}).")
        if not shared:
            return
    became_done = shared.get("status") == "Done"
    for other in group:
        if other["id"] == task["id"]:
            continue
        diff = {k: v for k, v in shared.items() if _cmp(k, other.get(k)) != _cmp(k, v)}
        if not diff:
            continue
        try:
            task_service.update_task(other["id"], TaskUpdate(**diff), other["user_id"], propagate=False)
        except Exception as exc:
            logger.warning("Копия %s не обновлена: %s", other["id"], type(exc).__name__)
            continue
        if became_done and other.get("status") != "Done":
            import notifier
            notifier.notify_user(other["user_id"], f"✅ «{task['task']}» — выполнено ({_name(actor_id)}).")


def leave(task: dict, user_id: str, notify: bool = True) -> None:
    """Перед удалением копии: шаги и комментарии переезжают к оставшимся, остальным — сообщение.
    Осталась одна копия — она становится обычной задачей (в группе остаётся)."""
    if not task.get("shared_id"):
        return
    others = [c for c in copies(task) if c["id"] != task["id"]]
    if not others:
        return
    canonical = task["shared_id"]
    if task["id"] == canonical:  # уходит главная копия — общие шаги и комментарии к следующей
        new = others[0]["id"]
        supabase.table("subtasks").update({"task_id": new}).eq("task_id", canonical).execute()
        supabase.table("task_comments").update({"task_id": new}).eq("task_id", canonical).execute()
        for c in others:
            supabase.table(TASKS).update({"shared_id": new}).eq("id", c["id"]).execute()
    if len(others) == 1:
        supabase.table(TASKS).update({"shared_id": None}).eq("id", others[0]["id"]).execute()
    if notify:
        import notifier
        for c in others:
            notifier.notify_user(c["user_id"], f"{_name(user_id)} больше не участвует в задаче «{task['task']}» — у вас она осталась.")
    logger.info("Выход из общей задачи %s", task["id"])


def info(user_id: str, tasks: list[dict]) -> dict:
    """{task_id: {"group": {id, name} | None, "with": [имена], "tick_all": bool, "done": n, "total": m}} —
    для значка «👥 Семья» и «1 из 3 готово»."""
    out = {}
    canon = {t["shared_id"] for t in tasks if t.get("shared_id")}
    group_ids = {t["group_id"] for t in tasks if t.get("group_id")}
    try:
        rows = supabase.table(TASKS).select("id,user_id,shared_id,status").in_("shared_id", list(canon)).execute().data if canon else []
        groups = {g["id"]: g for g in supabase.table("task_groups").select("id,name,tick_all").in_("id", list(group_ids)).execute().data} \
            if group_ids else {}
    except Exception as exc:  # до миграции 014
        logger.warning("Общие задачи недоступны: %s", type(exc).__name__)
        return {}
    names = {}
    for t in tasks:
        if not (t.get("shared_id") or t.get("group_id")):
            continue
        same = [r for r in rows if t.get("shared_id") and r["shared_id"] == t["shared_id"]] or [t]
        g = groups.get(t.get("group_id"))
        out[t["id"]] = {"group": {"id": g["id"], "name": g["name"]} if g else None,
                        "with": [names.setdefault(r["user_id"], _name(r["user_id"])) for r in same if r["user_id"] != user_id],
                        "tick_all": bool(g.get("tick_all", True)) if g else True,
                        "total": len(same), "done": sum(1 for r in same if r.get("status") == "Done")}
    return out
