"""
Цели накопления (решение владельца 10 октября 2026, миграция 011): «Отпуск — 1500 €», пополнение вручную.

Цель — money_goals (название, сумма, валюта, срок по желанию); каждое «Отложить» / «Снять» — строка
в money_goal_moves со знаком (+ отложил, − снял). Сколько отложено — сумма движений. Отложенное — не расход:
итоги месяца, лимиты и аналитика его не видят. Снять больше, чем отложено, нельзя.
Цель достигнута — achieved_at ставится один раз условным обновлением (повторные пополнения не поздравляют снова),
бот пишет поздравление. Суммы — Decimal, в JSON строкой, как везде в деньгах; «Отложено всего» — в основной валюте
по курсу Нацбанка (rates_service), approx — был ли пересчёт. Всё по user_id. В логи — только id и количества.
"""
import logging
import math
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import money_service
import rates_service
import users_service
from database import supabase
from money_service import MoneyError, fmt

logger = logging.getLogger(__name__)

GOALS = "money_goals"
MOVES = "money_goal_moves"
NAME_MAX = 40
HISTORY = 20


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _clean_name(value) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise MoneyError(422, f"Название цели — от 1 до {NAME_MAX} символов", "bad_name")
    return name


def _clean_due(value) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise MoneyError(422, "Дата цели в формате ГГГГ-ММ-ДД", "bad_date")
    if not date(2000, 1, 1) <= d <= date(2100, 1, 1):
        raise MoneyError(422, "Странная дата цели", "bad_date")
    return d.isoformat()


def _rows(user_id: str) -> list[dict]:
    return supabase.table(GOALS).select("*").eq("user_id", user_id).order("created_at").execute().data


def _get(goal_id: str, user_id: str) -> Optional[dict]:
    if not _is_uuid(goal_id):
        return None
    rows = supabase.table(GOALS).select("*").eq("id", goal_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def _saved_by_goal(user_id: str) -> dict:
    rows = money_service._fetch_all(lambda: supabase.table(MOVES).select("id,goal_id,amount").eq("user_id", user_id))
    out = {}
    for r in rows:
        out[r["goal_id"]] = out.get(r["goal_id"], Decimal(0)) + Decimal(str(r["amount"]))
    return out


def _per_month(left: Decimal, due: Optional[str], today: date) -> Optional[Decimal]:
    """Сколько откладывать в месяц, чтобы успеть к сроку (текущий месяц считается)."""
    if not due or left <= 0:
        return None
    d = date.fromisoformat(due)
    months = (d.year - today.year) * 12 + d.month - today.month + 1
    if months <= 0:
        return None
    return (left / months).quantize(Decimal("0.01"))


def _out(row: dict, saved: Decimal, today: date) -> dict:
    target = Decimal(str(row["target"]))
    left = max(Decimal(0), target - saved)
    return {"id": row["id"], "name": row["name"], "target": fmt(target), "currency": row["currency"],
            "due_date": str(row["due_date"])[:10] if row.get("due_date") else None,
            "saved": fmt(saved), "left": fmt(left),
            "percent": min(100, math.floor(saved / target * 100)) if target else 0,
            "per_month": fmt(_per_month(left, row.get("due_date") and str(row["due_date"])[:10], today))
            if _per_month(left, row.get("due_date") and str(row["due_date"])[:10], today) else None,
            "achieved": saved >= target}


def _today(user_id: str) -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(users_service.timezone_of(user_id))).date()


def list_goals(user_id: str) -> dict:
    """Цели (сначала незавершённые, по порядку создания) и «Отложено всего» в основной валюте."""
    today = _today(user_id)
    saved = _saved_by_goal(user_id)
    goals = [_out(r, saved.get(r["id"], Decimal(0)), today) for r in _rows(user_id)]
    goals.sort(key=lambda g: g["achieved"])
    main = money_service.default_currency(user_id)
    currencies = {g["currency"] for g in goals if Decimal(g["saved"]) != 0}
    rates = rates_service.get_rates() if currencies - {main} else None
    total = None
    if goals and rates_service.can_convert(rates, currencies, main):
        total = {"amount": fmt(sum((rates_service.convert(Decimal(g["saved"]), g["currency"], main, rates)
                                    for g in goals), Decimal(0))),
                 "currency": main, "approx": bool(currencies - {main})}
    return {"goals": goals, "total": total}


def create_goal(user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, "name": _clean_name(data.get("name")),
           "target": fmt(money_service.parse_amount(data.get("target"))),
           "currency": money_service.clean_currency(data.get("currency") or money_service.default_currency(user_id)),
           "due_date": _clean_due(data.get("due_date"))}
    goal = supabase.table(GOALS).insert(row).execute().data[0]
    if data.get("initial") not in (None, "", "0", 0):
        add_move(goal["id"], user_id, money_service.parse_amount(data["initial"]), notify=False)
    logger.info("Цель %s создана", goal["id"])
    return get_goal(goal["id"], user_id)


def get_goal(goal_id: str, user_id: str) -> dict:
    row = _get(goal_id, user_id)
    if not row:
        raise MoneyError(404, "Цель не найдена", "not_found")
    saved = _saved_by_goal(user_id).get(row["id"], Decimal(0))
    return _out(row, saved, _today(user_id))


def update_goal(goal_id: str, user_id: str, data: dict) -> dict:
    if not _get(goal_id, user_id):
        raise MoneyError(404, "Цель не найдена", "not_found")
    changes = {}
    if "name" in data:
        changes["name"] = _clean_name(data["name"])
    if "target" in data:
        changes["target"] = fmt(money_service.parse_amount(data["target"]))
    if "due_date" in data:
        changes["due_date"] = _clean_due(data["due_date"])
    if "currency" in data:
        cur = money_service.clean_currency(data["currency"])
        if cur != _get(goal_id, user_id)["currency"] and _saved_by_goal(user_id).get(goal_id):
            raise MoneyError(422, "Валюту можно поменять, только пока в цели ничего не отложено", "currency_locked")
        changes["currency"] = cur
    if changes:
        supabase.table(GOALS).update(changes).eq("id", goal_id).eq("user_id", user_id).execute()
    goal = get_goal(goal_id, user_id)
    if not goal["achieved"]:  # подняли сумму — цель снова в работе, поздравим ещё раз, когда дойдём
        supabase.table(GOALS).update({"achieved_at": None}).eq("id", goal_id).eq("user_id", user_id).execute()
    return goal


def delete_goal(goal_id: str, user_id: str) -> None:
    if not _get(goal_id, user_id):
        raise MoneyError(404, "Цель не найдена", "not_found")
    supabase.table(GOALS).delete().eq("id", goal_id).eq("user_id", user_id).execute()  # движения — каскадом


def add_move(goal_id: str, user_id: str, amount: Decimal, notify: bool = True) -> dict:
    """amount > 0 — отложить, < 0 — снять (не больше отложенного). Возвращает цель; first_achieved — только что
    достигнута (тогда бот поздравляет, если notify)."""
    row = _get(goal_id, user_id)
    if not row:
        raise MoneyError(404, "Цель не найдена", "not_found")
    saved = _saved_by_goal(user_id).get(goal_id, Decimal(0))
    if amount < 0 and -amount > saved:
        raise MoneyError(422, f"В цели отложено только {fmt(saved)} {row['currency']}", "too_much")
    supabase.table(MOVES).insert({"goal_id": goal_id, "user_id": user_id, "amount": fmt(amount)}).execute()
    goal = get_goal(goal_id, user_id)
    goal["first_achieved"] = False
    if goal["achieved"] and not row.get("achieved_at"):
        claimed = supabase.table(GOALS).update({"achieved_at": datetime.now(timezone.utc).isoformat()}) \
            .eq("id", goal_id).eq("user_id", user_id).is_("achieved_at", "null").execute().data
        goal["first_achieved"] = bool(claimed)
        if claimed and notify:
            import notifier
            notifier.notify_user(user_id, f"🎉 Цель «{row['name']}» достигнута: отложено "
                                          f"{goal['saved']} {row['currency']} из {goal['target']}!")
    logger.info("Цель %s: движение", goal_id)
    return goal


def moves(goal_id: str, user_id: str) -> list[dict]:
    if not _get(goal_id, user_id):
        raise MoneyError(404, "Цель не найдена", "not_found")
    rows = supabase.table(MOVES).select("*").eq("goal_id", goal_id).eq("user_id", user_id) \
        .order("created_at", desc=True).limit(HISTORY).execute().data
    return [{"id": r["id"], "amount": fmt(r["amount"]), "created_at": r["created_at"]} for r in rows]


def find_goal(user_id: str, text: str) -> list[dict]:
    """Цели по началу или части названия (без учёта регистра, ё = е) — для /save в боте."""
    norm = lambda s: re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()
    q = norm(text)
    active = [r for r in list_goals(user_id)["goals"]]
    if not q:
        return active
    exact = [g for g in active if norm(g["name"]) == q]
    return exact or [g for g in active if q in norm(g["name"])]
