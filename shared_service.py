"""
Совместные задачи (миграция 014, решение владельца 11 октября 2026): «задача на нас двоих».

Автор выбирает «С кем» → второму приходит приглашение в бот (кнопки «Принять / Отклонить», `sh:y|n:<id>`).
Приняли — у второго появляется своя копия задачи. У всех копий shared_id = id главной копии (на ней лежат общие
шаги и комментарии: subtasks_service и comments_service ищут их по главной копии). У каждого своя категория
«С <имя>» (ставится сама), свой календарь, свои напоминания.

Решения владельца: название, срок, «до», место, заметка, приоритет и статус меняются у всех копий; галочка
закрывает задачу у всех (остальным приходит сообщение); удаление — выход из задачи: у остальных она остаётся
(если осталась одна копия — становится обычной). Повтор у совместной задачи не поддерживается (422).
Поделиться можно только со своими контактами (contacts_service). Один человек — напрямую; двое и больше — только
группой: тогда у всех категория с названием группы и у каждого своя галочка (tasks.shared_each, «1 из 3 готово»).
Все проверки по user_id: копию меняет только её владелец, ответить на приглашение — только тот, кому оно.
В логи — только id.
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
INVITES = "task_invites"
SHARED_FIELDS = ("task", "deadline", "ends_at", "note", "location", "priority", "status")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(user_id: str) -> str:
    return (users_service.get_by_id(user_id) or {}).get("name") or "Без имени"


def partners(user_id: str) -> list[dict]:
    """С кем можно поделиться задачей: только свои контакты (взаимное согласие, contacts_service)."""
    import contacts_service
    return contacts_service.contacts(user_id)


def partner_category(user_id: str, partner_name: str) -> str:
    """Категория «С <имя>» у пользователя (создаётся при первой совместной задаче)."""
    return ensure_category(user_id, f"С {partner_name}")


def ensure_category(user_id: str, name: str) -> str:
    """Категория с таким названием у пользователя (создаётся, если её нет)."""
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
    """Все копии совместной задачи (включая эту). Обычная задача — только она сама."""
    if not task.get("shared_id"):
        return [task]
    return supabase.table(TASKS).select("*").eq("shared_id", task["shared_id"]).execute().data or [task]


def canonical_id(task: dict) -> str:
    return task.get("shared_id") or task["id"]


def own_copy(canonical: str, user_id: str) -> Optional[dict]:
    """Копия пользователя для главной копии canonical (или сама задача, если она его и не совместная)."""
    rows = supabase.table(TASKS).select("*").eq("id", canonical).eq("user_id", user_id).execute().data
    if rows:
        return rows[0]
    rows = supabase.table(TASKS).select("*").eq("shared_id", canonical).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def invite(task_id: str, from_user: str, to_user: Optional[str] = None, group_id: Optional[str] = None) -> dict:
    """Пригласить в свою задачу одного контакта (to_user) или группу (group_id).
    Возвращает {"invite_ids", "status"}; ошибки — CategoryError(4xx)."""
    import contacts_service
    import task_service
    task = task_service.get_task(task_id, from_user)
    if not task:
        raise CategoryError(404, "Задача не найдена", "not_found")
    if task.get("status") == "Done":
        raise CategoryError(422, "Задача уже выполнена", "done")
    if task.get("is_recurring"):
        raise CategoryError(422, "Повторяющейся задачей пока нельзя поделиться — снимите повтор", "recurring")
    if task.get("money_recurring_id") or task.get("gcal_imported"):
        raise CategoryError(422, "Этой задачей поделиться нельзя", "not_shareable")
    others = {c["user_id"] for c in copies(task)} - {from_user}
    pending = supabase.table(INVITES).select("*").eq("task_id", task_id).execute().data
    waiting = {r["to_user"] for r in pending if r["status"] == "pending"}
    if group_id:
        group = contacts_service.get_group(group_id, from_user)
        if not group:
            raise CategoryError(404, "Группа не найдена", "no_group")
        targets = [m["id"] for m in group["members"] if contacts_service.is_contact(from_user, m["id"])]
        if len(targets) < 2:
            raise CategoryError(422, "В группе нужно хотя бы двое ваших контактов", "too_few")
        if others or waiting:
            raise CategoryError(409, "Задача уже общая — группу можно выбрать только для новой", "already")
        group_name = group["name"]
        text = f"👥 {_name(from_user)} предлагает задачу группе «{group_name}»:\n{task['task']}{_when(task)}"
    else:
        if not to_user or to_user == from_user or not contacts_service.is_contact(from_user, to_user):
            raise CategoryError(404, "Такого человека нет в ваших контактах", "no_partner")
        if to_user in others:
            raise CategoryError(409, "Задача уже общая с этим человеком", "already")
        if task.get("shared_each") or (others | waiting) - {to_user}:
            raise CategoryError(409, "Для троих и больше создайте группу и поделитесь задачей с ней", "use_group")
        targets, group_name = [to_user], None
        text = f"👥 {_name(from_user)} предлагает задачу на двоих:\n{task['task']}{_when(task)}"
    cat = ensure_category(from_user, group_name) if group_name else partner_category(from_user, _name(to_user))
    changes = {"category": cat, "updated_at": _now()}
    if group_name:
        changes["shared_each"] = True
    supabase.table(TASKS).update(changes).eq("id", task_id).eq("user_id", from_user).execute()
    import notifier
    ids = []
    for target in targets:
        rows = [r for r in pending if r["to_user"] == target]
        if rows and rows[0]["status"] == "pending":
            inv = rows[0]
        elif rows:  # отклонил раньше — спрашиваем ещё раз
            supabase.table(INVITES).update({"status": "pending", "answered_at": None, "created_at": _now(),
                                            "group_name": group_name}).eq("id", rows[0]["id"]).execute()
            inv = rows[0]
        else:
            row = {"task_id": task_id, "from_user": from_user, "to_user": target}
            if group_name:
                row["group_name"] = group_name
            inv = supabase.table(INVITES).insert(row).execute().data[0]
        ids.append(inv["id"])
        notifier.notify_user(target, text, buttons=[[{"text": "✅ Принять", "callback_data": f"sh:y:{inv['id']}"},
                                                     {"text": "✖️ Отклонить", "callback_data": f"sh:n:{inv['id']}"}]])
    logger.info("Задача %s: приглашений %d", task_id, len(ids))
    return {"invite_ids": ids, "invite_id": ids[0], "status": "pending"}


def _when(task: dict) -> str:
    if not task.get("deadline"):
        return ""
    d = datetime.fromisoformat(str(task["deadline"]).replace("Z", "+00:00")).replace(tzinfo=None)
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    time = "" if d.strftime("%H:%M") == "23:59" else d.strftime(" · %H:%M")
    return f"\n{days[d.weekday()]}, {d.strftime('%d.%m')}{time}"


def answer(invite_id: str, user_id: str, accept: bool) -> str:
    """Ответ на приглашение (только тот, кому оно). Возвращает текст для сообщения в боте."""
    import task_service
    rows = supabase.table(INVITES).select("*").eq("id", invite_id).eq("to_user", user_id).execute().data
    if not rows:
        return "Приглашение не найдено — возможно, задачу уже удалили."
    inv = rows[0]
    claimed = supabase.table(INVITES).update({"status": "accepted" if accept else "declined", "answered_at": _now()}) \
        .eq("id", invite_id).eq("to_user", user_id).eq("status", "pending").execute().data
    if not claimed:
        return "Вы уже ответили на это приглашение."
    source = task_service.get_task(inv["task_id"], inv["from_user"])
    if not source:
        return "Эту задачу уже удалили."
    import notifier
    me = _name(user_id)
    if not accept:
        notifier.notify_user(inv["from_user"], f"Приглашение в задачу «{source['task']}» отклонено ({me}).")
        return f"Хорошо, задача «{source['task']}» к вам не добавлена."
    canonical = canonical_id(source)
    if not source.get("shared_id"):
        supabase.table(TASKS).update({"shared_id": canonical}).eq("id", source["id"]).eq("user_id", inv["from_user"]).execute()
    category = ensure_category(user_id, inv["group_name"]) if inv.get("group_name") \
        else partner_category(user_id, _name(inv["from_user"]))
    row = {"user_id": user_id, "task": source["task"], "category": category,
           "source": "Совместная", "deadline": source.get("deadline"), "ends_at": source.get("ends_at"),
           "priority": source.get("priority") or "Mid", "status": "To Do", "note": source.get("note"),
           "is_recurring": False, "tags": [], "shared_id": canonical}
    if source.get("location"):
        row["location"] = source["location"]
    if source.get("shared_each"):
        row["shared_each"] = True
    copy = supabase.table(TASKS).insert(row).execute().data[0]
    task_service._sync_google_upsert(copy, user_id)
    notifier.notify_user(inv["from_user"], f"👥 {me} принимает задачу «{source['task']}»." if inv.get("group_name")
                         else f"👥 Задача «{source['task']}» теперь общая с {me}.")
    logger.info("Приглашение %s принято, копия %s", invite_id, copy["id"])
    return f"👥 Задача «{source['task']}» добавлена вам — в категории «{copy['category']}»."


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
    if task.get("shared_each"):  # группа: у каждого своя галочка — остальным только «готово N из M»
        status = shared.pop("status", None)
        if status == "Done" and task.get("status") != "Done":
            group = copies(task)
            done = sum(1 for c in group if c.get("status") == "Done")
            import notifier
            for c in group:
                if c["user_id"] != actor_id:
                    notifier.notify_user(c["user_id"], f"✅ «{task['task']}»: {_name(actor_id)} — готово ({done} из {len(group)}).")
        if not shared:
            return
    became_done = shared.get("status") == "Done"
    for other in copies(task):
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


def leave(task: dict, user_id: str) -> None:
    """Перед удалением копии: шаги и комментарии переезжают к оставшимся, остальным — сообщение.
    Осталась одна копия — она становится обычной задачей."""
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
        canonical = new
    if len(others) == 1:
        supabase.table(TASKS).update({"shared_id": None, "shared_each": False}).eq("id", others[0]["id"]).execute()
    import notifier
    for c in others:
        notifier.notify_user(c["user_id"], f"{_name(user_id)} больше не участвует в задаче «{task['task']}» — у вас она осталась.")
    logger.info("Выход из совместной задачи %s", task["id"])


def info(user_id: str, tasks: list[dict]) -> dict:
    """{task_id: {"with": [имена], "pending": [имена], "each": bool, "done": n, "total": m}} — для значка «👥»,
    «ждёт …» и «1 из 3 готово» у групповой задачи."""
    out = {}
    canon = {t["shared_id"] for t in tasks if t.get("shared_id")}
    names = {}
    if canon:
        rows = supabase.table(TASKS).select("id,user_id,shared_id,status").in_("shared_id", list(canon)).execute().data
        for t in tasks:
            if t.get("shared_id"):
                group = [r for r in rows if r["shared_id"] == t["shared_id"]]
                others = [r["user_id"] for r in group if r["user_id"] != user_id]
                out[t["id"]] = {"with": [names.setdefault(u, _name(u)) for u in others], "pending": [],
                                "each": bool(t.get("shared_each")), "total": len(group),
                                "done": sum(1 for r in group if r.get("status") == "Done")}
    try:
        pending = supabase.table(INVITES).select("task_id,to_user").eq("from_user", user_id).eq("status", "pending").execute().data
    except Exception as exc:  # до миграции 014 таблицы нет
        logger.warning("Приглашения недоступны: %s", type(exc).__name__)
        pending = []
    by_id = {t["id"]: t for t in tasks}
    for p in pending:
        if p["task_id"] not in by_id:
            continue
        entry = out.setdefault(p["task_id"], {"with": [], "pending": [], "each": bool(by_id[p["task_id"]].get("shared_each")),
                                              "total": 1, "done": 0})
        entry["pending"].append(names.setdefault(p["to_user"], _name(p["to_user"])))
    return out
