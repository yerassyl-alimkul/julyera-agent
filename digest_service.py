"""
Сводки и напоминания по расписанию пользователя (шаг 7): /today, утренняя и вечерняя сводка,
напоминания о дедлайнах с кнопками «Готово» / «Отложить на час», перенос невыполненного на завтра.

Расписание считается по поясу каждого пользователя (users.timezone). /internal/reminders/run можно звать
сколько угодно часто (cron-job.org каждые 15 минут, плюс прежние запуски GitHub Actions): каждая рассылка
уходит в своё окно [время, время + 1 час) и не больше одного раза в день — перед отправкой в digest_log
вставляется строка (user_id, kind, day), а повторная вставка упирается в первичный ключ.
Отложенная задача (tasks.snoozed_until) напоминается один раз, когда срок наступил; поле при этом очищается.

Изоляция как везде: всё по user_id; задача из кнопки проверяется на принадлежность нажавшему.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import task_service
import users_service
from database import supabase
from models import Status, TaskUpdate

logger = logging.getLogger(__name__)

LOG_TABLE = "digest_log"
TASKS = "tasks"

# Окна рассылок по местному времени пользователя: (вид, время). Окно — час после указанного времени.
SLOTS = [("morning", time(8, 0)), ("remind_1", time(13, 0)), ("remind_2", time(17, 0)), ("evening", time(20, 0))]
WINDOW = timedelta(hours=1)
SNOOZE = timedelta(hours=1)
REMINDER_DAYS_AHEAD = 2   # как раньше: дедлайн до «сегодня + 2 дня» по дате
REMINDER_BUTTONS_MAX = 10  # строк с кнопками в одном напоминании (остальные задачи — просто списком)

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


# ---------------------------------------------------------------- время

def utc_now() -> datetime:
    """Текущий момент; проверки подменяют эту функцию, чтобы поставить нужное местное время."""
    return datetime.now(timezone.utc)


def tz_of(user_id: str) -> ZoneInfo:
    return ZoneInfo(users_service.timezone_of(user_id))


def local_now(user_id: str, now_utc: Optional[datetime] = None) -> datetime:
    """Местное время пользователя без tzinfo (как «голые часы» дедлайнов)."""
    now_utc = now_utc or utc_now()
    return now_utc.astimezone(tz_of(user_id)).replace(tzinfo=None)


def _deadline(task: dict) -> Optional[datetime]:
    """Дедлайн — «голые часы» (метку +00:00 от Supabase отбрасываем, см. CLAUDE.md)."""
    value = task.get("deadline")
    return datetime.fromisoformat(value).replace(tzinfo=None) if value else None


def _aware_utc(value) -> Optional[datetime]:
    """Настоящий момент времени (snoozed_until, updated_at) → aware UTC; без пояса считаем UTC."""
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_slot(local: datetime) -> Optional[str]:
    for kind, at in SLOTS:
        start = datetime.combine(local.date(), at)
        if start <= local < start + WINDOW:
            return kind
    return None


def short_date(d: date) -> str:
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


# ---------------------------------------------------------------- выборки

def _open_tasks(user_id: str) -> list[dict]:
    return [t for t in task_service.list_tasks(user_id) if t.get("status") != Status.done.value]


def _is_snoozed(task: dict, now_utc: datetime) -> bool:
    until = _aware_utc(task.get("snoozed_until"))
    return bool(until and until > now_utc)


def today_view(user_id: str, now_utc: Optional[datetime] = None) -> dict:
    """Просроченные, на сегодня и в ближайшие 24 часа (после сегодняшнего дня) — только задачи пользователя."""
    local = local_now(user_id, now_utc)
    today = local.date()
    horizon = local + timedelta(hours=24)
    view = {"today": today, "overdue": [], "today_tasks": [], "next24": []}
    for t in _open_tasks(user_id):
        d = _deadline(t)
        if not d:
            continue
        if d.date() < today:
            view["overdue"].append(t)
        elif d.date() == today:
            view["today_tasks"].append(t)
        elif d <= horizon:
            view["next24"].append(t)
    for key in ("overdue", "today_tasks", "next24"):
        view[key].sort(key=lambda t: t["deadline"])
    return view


def reminder_tasks(user_id: str, now_utc: Optional[datetime] = None) -> list[dict]:
    """Незакрытые задачи с дедлайном до «сегодня + 2 дня» по дате пользователя, кроме отложенных."""
    now_utc = now_utc or utc_now()
    cutoff = local_now(user_id, now_utc).date() + timedelta(days=REMINDER_DAYS_AHEAD)
    out = [t for t in _open_tasks(user_id)
           if _deadline(t) and _deadline(t).date() <= cutoff and not _is_snoozed(t, now_utc)]
    return sorted(out, key=lambda t: t["deadline"])


def done_today(user_id: str, now_utc: Optional[datetime] = None) -> list[dict]:
    """Закрытые сегодня (по местной дате updated_at: отдельной даты закрытия у задач нет)."""
    tz = tz_of(user_id)
    today = local_now(user_id, now_utc).date()
    out = []
    for t in task_service.list_tasks(user_id, status=Status.done.value):
        changed = _aware_utc(t.get("updated_at"))
        if changed and changed.astimezone(tz).date() == today:
            out.append(t)
    return out


def has_any_tasks(user_id: str) -> bool:
    return bool(supabase.table(TASKS).select("id").eq("user_id", user_id).limit(1).execute().data)


# ---------------------------------------------------------------- тексты

def _time_or_date(t: dict, today: date) -> str:
    d = _deadline(t)
    if not d:
        return ""
    hm = d.strftime("%H:%M")
    if d.date() == today:
        return hm
    if d.date() == today + timedelta(days=1):
        return f"завтра {hm}"
    return f"{short_date(d.date())} {hm}"


def _grouped(tasks: list[dict], today: date) -> list[str]:
    """Строка на задачу, сгруппировано по категориям (категории в порядке первого появления)."""
    lines, by_cat = [], {}
    for t in tasks:
        by_cat.setdefault(t.get("category") or "Без категории", []).append(t)
    for cat, items in by_cat.items():
        lines.append(f"  {cat}:")
        for t in items:
            when = _time_or_date(t, today)
            lines.append(f"    • {when + ' ' if when else ''}{t['task']}")
    return lines


def today_text(user_id: str, now_utc: Optional[datetime] = None, title: Optional[str] = None) -> str:
    v = today_view(user_id, now_utc)
    today = v["today"]
    lines = [title or f"📋 На {today.day} {MONTHS_GEN[today.month - 1]}"]
    if not (v["overdue"] or v["today_tasks"] or v["next24"]):
        lines.append("На сегодня задач нет — свободный день 🌿")
        return "\n".join(lines)
    for key, label in (("overdue", "Просрочено"), ("today_tasks", "Сегодня"), ("next24", "Ближайшие 24 часа")):
        if v[key]:
            lines.append(f"\n{label}:")
            lines += _grouped(v[key], today)
    return "\n".join(lines)


def _yesterday_spent_line(user_id: str, today: date) -> Optional[str]:
    """«Вчера потрачено: 23.50 EUR · 4000.00 KZT» — если в трекере денег есть расходы за вчера."""
    try:
        import money_service
        from decimal import Decimal

        day = (today - timedelta(days=1)).isoformat()
        rows = supabase.table(money_service.TX_TABLE).select("amount,currency").eq("user_id", user_id).eq(
            "kind", "expense").eq("spent_on", day).execute().data
        totals = {}
        for r in rows:
            totals[r["currency"]] = totals.get(r["currency"], Decimal(0)) + Decimal(str(r["amount"]))
        if not totals:
            return None
        parts = [f"{money_service.fmt(totals[c])} {c}" for c in money_service.CURRENCIES if c in totals]
        return "💸 Вчера потрачено: " + " · ".join(parts)
    except Exception as exc:  # трекер денег не обязателен для сводки
        logger.warning("Траты за вчера для сводки не посчитаны: %s", type(exc).__name__)
        return None


def morning_text(user_id: str, now_utc: Optional[datetime] = None) -> str:
    local = local_now(user_id, now_utc)
    text = today_text(user_id, now_utc, title=f"☀️ Доброе утро! План на {local.day} {MONTHS_GEN[local.month - 1]}")
    spent = _yesterday_spent_line(user_id, local.date())
    return text + (f"\n\n{spent}" if spent else "")


def evening(user_id: str, now_utc: Optional[datetime] = None) -> tuple[str, bool]:
    """Текст вечерней сводки и есть ли что переносить (тогда под ней кнопка «Перенести невыполненное»)."""
    v = today_view(user_id, now_utc)
    today = v["today"]
    done = done_today(user_id, now_utc)
    unfinished = v["overdue"] + v["today_tasks"]
    tomorrow = [t for t in _open_tasks(user_id) if _deadline(t) and _deadline(t).date() == today + timedelta(days=1)]
    lines = ["🌙 Итоги дня"]
    lines.append(f"\n✅ Выполнено: {len(done)}" if done else "\nСегодня ничего не закрыто.")
    for t in done[:10]:
        lines.append(f"    • {t['task']}")
    if unfinished:
        lines.append("\nНе закрыто:")
        lines += _grouped(sorted(unfinished, key=lambda t: t["deadline"]), today)
    if tomorrow:
        lines.append("\nЗавтра:")
        lines += _grouped(sorted(tomorrow, key=lambda t: t["deadline"]), today)
    return "\n".join(lines), bool(unfinished)


def reminder_message(user_id: str, tasks: list[dict], now_utc: Optional[datetime] = None) -> tuple[str, list]:
    """Текст напоминания и строки кнопок: по строке «✅ задача» / «⏰ +1 ч» на задачу (не больше 10)."""
    today = local_now(user_id, now_utc).date()
    lines = ["⏰ Напоминание о дедлайнах:"]
    for t in tasks:
        when = _time_or_date(t, today)
        overdue = _deadline(t).date() < today
        lines.append(f"{'🔴' if overdue else '•'} {t['task']} · {t.get('category', '')} — {'просрочено, ' if overdue else ''}{when}")
    rows = []
    for t in tasks[:REMINDER_BUTTONS_MAX]:
        row = [{"text": "✅ " + _short(t["task"]), "callback_data": f"done:{t['id']}"}]
        if _deadline(t).date() < today:  # просрочена — перенести на завтра или послезавтра
            row += [{"text": "➡️ Завтра", "callback_data": f"mv1:{t['id']}"},
                    {"text": "➡️ +2 дня", "callback_data": f"mv2:{t['id']}"}]
        else:
            row.append({"text": "⏰ +1 ч", "callback_data": f"snooze:{t['id']}"})
        rows.append(row)
    return "\n".join(lines), rows


def _short(text: str, n: int = 24) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


# ---------------------------------------------------------------- рассылка (вызывает /internal/reminders/run)

def claim(user_id: str, kind: str, day: date) -> bool:
    """Отметка «отправлено» ДО отправки. False — уже было сегодня (или вставка не удалась)."""
    try:
        supabase.table(LOG_TABLE).insert({"user_id": user_id, "kind": kind, "day": day.isoformat()}).execute()
        return True
    except Exception as exc:
        if getattr(exc, "code", None) != "23505" and "duplicate key" not in str(exc):
            logger.warning("digest_log: вставка не удалась (%s)", type(exc).__name__)
        return False


def due_messages(user: dict, now_utc: Optional[datetime] = None) -> list[tuple[str, Optional[list]]]:
    """Что отправить пользователю в этот запуск: [(текст, строки кнопок или None)].
    Сводки — если включены и у пользователя вообще есть задачи; напоминания — если есть что напомнить;
    плюс отложенные задачи, чей срок наступил (каждая — отдельным сообщением с кнопками)."""
    now_utc = now_utc or utc_now()
    uid = user["id"]
    out = []
    local = local_now(uid, now_utc)
    slot = current_slot(local)
    if slot in ("morning", "evening"):
        if user.get("digest_enabled", True) is not False and has_any_tasks(uid) and claim(uid, slot, local.date()):
            if slot == "morning":
                out.append((morning_text(uid, now_utc), None))
                report = monthly_report(uid, local.date())
                if report:
                    out.append(report)
            else:
                text, can_move = evening(uid, now_utc)
                buttons = [[{"text": "➡️ Перенести невыполненное на завтра",
                             "callback_data": f"dmove:{local.date().isoformat()}"}]] if can_move else None
                out.append((text, buttons))
    matured = matured_snoozes(uid, now_utc)
    in_slot = set()
    if slot in ("remind_1", "remind_2"):
        tasks = reminder_tasks(uid, now_utc)  # срок отложенных уже наступил — они здесь тоже есть
        if tasks and claim(uid, slot, local.date()):
            out.append(reminder_message(uid, tasks, now_utc))
            in_slot = {t["id"] for t in tasks}
    for task in matured:
        if task["id"] not in in_slot:  # не дублируем то, что уже есть в плановом напоминании
            out.append(reminder_message(uid, [task], now_utc))
    return out


def monthly_report(user_id: str, today: date) -> Optional[tuple[str, list]]:
    """1-го числа вместе с утренней сводкой — итоги прошедшего месяца по деньгам (один раз, money_report_log).
    Нет операций за месяц — отчёта нет."""
    if today.day != 1:
        return None
    try:
        import money_service

        prev = (today - timedelta(days=1)).replace(day=1)
        text = money_service.monthly_report_text(user_id, prev)
        if not text or not money_service.claim_monthly_report(user_id, prev.strftime("%Y-%m")):
            return None
        from notifier import APP_URL
        return text, [[{"text": "Открыть аналитику", "web_app": {"url": APP_URL}}]]
    except Exception as exc:
        logger.warning("Месячный отчёт не собран: %s", type(exc).__name__)
        return None


def matured_snoozes(user_id: str, now_utc: datetime) -> list[dict]:
    """Отложенные задачи пользователя, срок которых наступил. Поле snoozed_until очищается условным
    обновлением (eq по старому значению), поэтому два одновременных запуска не напомнят дважды."""
    rows = supabase.table(TASKS).select("*").eq("user_id", user_id).lte("snoozed_until", now_utc.isoformat()).execute().data
    out = []
    for t in rows:
        if t.get("status") == Status.done.value:
            continue
        claimed = supabase.table(TASKS).update({"snoozed_until": None}).eq("id", t["id"]).eq(
            "user_id", user_id).eq("snoozed_until", t["snoozed_until"]).execute().data
        if claimed:
            out.append(t)
    return out


# ---------------------------------------------------------------- действия кнопок

def complete(task_id: str, user_id: str) -> tuple[str, Optional[dict]]:
    """«Готово»: тот же путь, что у веба (повтор задачи, календарь). ('done'|'already'|'not_found', задача)."""
    task = task_service.get_task(task_id, user_id)
    if not task:
        return "not_found", None
    if task.get("status") == Status.done.value:
        return "already", task
    supabase.table(TASKS).update({"snoozed_until": None}).eq("id", task_id).eq("user_id", user_id).execute()
    return "done", task_service.update_task(task_id, TaskUpdate(status=Status.done), user_id)


def snooze(task_id: str, user_id: str, now_utc: Optional[datetime] = None) -> tuple[str, Optional[datetime]]:
    """«Отложить на час»: snoozed_until = сейчас + 1 час. Возвращает (статус, местное время окончания)."""
    now_utc = now_utc or utc_now()
    task = task_service.get_task(task_id, user_id)
    if not task:
        return "not_found", None
    if task.get("status") == Status.done.value:
        return "already", None
    until = now_utc + SNOOZE
    supabase.table(TASKS).update({"snoozed_until": until.isoformat()}).eq("id", task_id).eq("user_id", user_id).execute()
    return "snoozed", local_now(user_id, until)


def move_unfinished(user_id: str, day: date, now_utc: Optional[datetime] = None) -> tuple[str, int]:
    """«Перенести невыполненное на завтра»: незакрытые задачи с дедлайном до этого дня включительно
    получают дедлайн завтра в то же время. Только для сегодняшней сводки ('stale' для старой)."""
    today = local_now(user_id, now_utc).date()
    if day != today:
        return "stale", 0
    tomorrow = today + timedelta(days=1)
    moved = 0
    for t in _open_tasks(user_id):
        d = _deadline(t)
        if d and d.date() <= today:
            task_service.update_task(t["id"], TaskUpdate(deadline=datetime.combine(tomorrow, d.time())), user_id)
            moved += 1
    return ("moved" if moved else "nothing"), moved


def reschedule(task_id: str, user_id: str, days: int) -> tuple[str, Optional[dict]]:
    """«➡️ Завтра / Послезавтра» у просроченной задачи: дедлайн = сегодня + days, время прежнее.
    Через task_service.update_task — событие в календаре переезжает. ('moved'|'already'|'not_found', задача)."""
    task = task_service.get_task(task_id, user_id)
    if not task:
        return "not_found", None
    if task.get("status") == Status.done.value:
        return "already", task
    d = _deadline(task)
    day = local_now(user_id).date() + timedelta(days=days)
    new = datetime.combine(day, d.time() if d else time(23, 59))
    supabase.table(TASKS).update({"snoozed_until": None}).eq("id", task_id).eq("user_id", user_id).execute()
    return "moved", task_service.update_task(task_id, TaskUpdate(deadline=new), user_id)


def set_enabled(user_id: str, enabled: bool) -> None:
    supabase.table("users").update({"digest_enabled": bool(enabled)}).eq("id", user_id).execute()
