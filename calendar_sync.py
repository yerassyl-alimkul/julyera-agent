"""
Синхронизация из Google Calendar в задачи (обратное направление к google_calendar_service).

Зовётся из /internal/reminders/run (каждые 15 минут), сразу после подключения календаря и после выбора календарей.
Для каждого пользователя с подключённым календарём читает выбранные им календари (google_credentials.
import_calendars, по умолчанию только основной; id календаря у задачи — tasks.gcal_calendar_id, пусто = основной)
с начала сегодняшнего дня:
- разовые события — все будущие;
- повторяющиеся — каждое повторение отдельной задачей, но только на RECURRING_DAYS вперёд (окно сдвигается
  каждый день, новые повторения появляются сами);
и дальше:
- событие, которого ещё нет среди задач, становится задачей (gcal_imported, категорию подбирает Groq по названию);
- событие, изменённое в Google после последней синхронизации (updated > tasks.gcal_updated), обновляет задачу:
  название, время, описание → заметка;
- событие, удалённое в Google, удаляет незакрытую задачу (закрытая остаётся в архиве без ссылки).
Удаляем только при явном признаке (status = cancelled или 404/410 на запрос события), не по отсутствию в списке.

Выключатель в меню приложения (users.calendar_import = false, миграция 007) останавливает это направление
целиком: ни новых задач, ни правок и удалений из календаря; уже перенесённые задачи остаются.

События, созданные самим JulYera (метка JULYERA_MARK или префикс-эмодзи приоритета у старых), задачами
не становятся, а прошлые события не переносятся. В логи — только количества.
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import categories_service
import comments_service
import google_calendar_service as gcal
import users_service
from database import supabase
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

TABLE = "tasks"
RECURRING_DAYS = 30      # повторения повторяющихся событий — на месяц вперёд
PAGE_SIZE = 250
MAX_PAGES = 8            # больше 2000 событий за раз не читаем (и тогда не проверяем пропавшие)
MAX_LOOKUPS = 20         # проверок «событие удалено?» за один проход
SKIP_TYPES = {"workingLocation"}  # «где я работаю сегодня» — не событие
SOURCE = "Google Calendar"
TITLE_MAX = 300
NOTE_MAX = 4000
_JULYERA_PREFIX = re.compile(r"^(?:[" + "".join(sorted(set(gcal.PRIORITY_EMOJI.values()) |
                                                      set(gcal.CATEGORY_EMOJI.values()))) + r"]️?\s*)+")
FIRST_IMPORT_TEXT = ("📅 Перенёс из Google Calendar событий: {n}. Теперь новые события из календаря будут "
                     "появляться в задачах сами, а изменения — переноситься в обе стороны.")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _naive(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def event_deadline(event: dict, tz: str) -> tuple[Optional[datetime], bool]:
    """Начало события как «голые» часы в поясе пользователя (договорённость №1). Весь день — дата в 23:59."""
    start = event.get("start") or {}
    if start.get("dateTime"):
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        return dt.replace(second=0, microsecond=0), False
    if start.get("date"):
        return datetime.fromisoformat(start["date"]).replace(hour=23, minute=59), True
    return None, False


def _plain(description: Optional[str]) -> Optional[str]:
    """Описание события (Google хранит его с HTML) → текст заметки."""
    if not description:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", description, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text[:NOTE_MAX] or None


def _title(event: dict, julyera_made: bool) -> str:
    title = (event.get("summary") or "").strip()
    if julyera_made:
        title = _JULYERA_PREFIX.sub("", title).strip()
    return title[:TITLE_MAX] or "(без названия)"


def is_julyera_event(event: dict) -> bool:
    if ((event.get("extendedProperties") or {}).get("private") or {}).get("julyera") == "1":
        return True
    # события, созданные до метки: название начинается с эмодзи приоритета 🔴 🟡 🟢
    summary = (event.get("summary") or "").lstrip()
    return any(summary.startswith(e) for e in gcal.PRIORITY_EMOJI.values()) or \
        any(summary.startswith(e) for e in gcal.CATEGORY_EMOJI.values())


def _declined(event: dict) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees") or [])


def _list(service, calendar_id: str, **params) -> tuple[list[dict], bool]:
    """Все страницы списка событий. Второе значение — прочитан ли список целиком."""
    items, token = [], None
    for _ in range(MAX_PAGES):
        page = service.events().list(calendarId=calendar_id, showDeleted=True, maxResults=PAGE_SIZE,
                                     pageToken=token, **params).execute()
        items.extend(page.get("items") or [])
        token = page.get("nextPageToken")
        if not token:
            return items, True
    return items, False


def fetch_events(service, tz: str, now_utc: Optional[datetime] = None, calendar_id: str = "primary") -> tuple[list[dict], bool]:
    """События календаря с начала сегодняшнего дня: всё в окне RECURRING_DAYS (повторения раскрыты)
    и разовые события дальше окна."""
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = day_start + timedelta(days=RECURRING_DAYS)
    near, ok_near = _list(service, calendar_id, singleEvents=True, timeMin=day_start.isoformat(), timeMax=window_end.isoformat())
    far, ok_far = _list(service, calendar_id, singleEvents=False, timeMin=window_end.isoformat())
    seen, events = set(), []
    for ev in near + [e for e in far if not e.get("recurrence") and not e.get("recurringEventId")]:
        if ev.get("id") and ev["id"] not in seen:
            seen.add(ev["id"])
            events.append(ev)
    return events, ok_near and ok_far


def _linked_tasks(user_id: str) -> dict:
    rows = supabase.table(TABLE).select("*").eq("user_id", user_id).execute().data
    return {r["google_event_id"]: r for r in rows if r.get("google_event_id")}


def _drop_task(task: dict, user_id: str) -> str:
    """Событие удалено в Google: незакрытую задачу удаляем, закрытую оставляем в архиве без ссылки."""
    if task.get("status") == "Done":
        supabase.table(TABLE).update({"google_event_id": None, "gcal_imported": False, "gcal_calendar_id": None}) \
            .eq("id", task["id"]).eq("user_id", user_id).execute()
        return "unlinked"
    comments_service.delete_task_files(task["id"], user_id)
    supabase.table(TABLE).delete().eq("id", task["id"]).eq("user_id", user_id).execute()
    return "deleted"


def _apply_event(task: dict, event: dict, user_id: str, tz: str) -> bool:
    """Событие менялось в Google после нашей последней синхронизации — переносим изменения в задачу.
    Без записи gcal_updated (старые задачи до этой версии) просто запоминаем отметку, ничего не меняя."""
    updated = event.get("updated")
    seen = _parse_ts(task.get("gcal_updated"))
    if seen and _parse_ts(updated) and _parse_ts(updated) <= seen:
        return False
    changes = {"gcal_updated": updated}
    if seen or task.get("gcal_imported"):
        title = _title(event, julyera_made=not task.get("gcal_imported"))
        if title != task.get("task"):
            changes["task"] = title
        deadline, all_day = event_deadline(event, tz)
        if deadline and deadline != _naive(task.get("deadline")):
            changes["deadline"] = deadline.isoformat()
        if task.get("gcal_imported") and all_day != bool(task.get("gcal_all_day")):
            changes["gcal_all_day"] = all_day
        note = _plain(event.get("description"))
        if note != (task.get("note") or None):
            changes["note"] = note
    real = set(changes) - {"gcal_updated"}
    if real:
        changes["updated_at"] = datetime.utcnow().isoformat()
    supabase.table(TABLE).update(changes).eq("id", task["id"]).eq("user_id", user_id).execute()
    return bool(real)


def _categories(user_id: str, titles: list[str]) -> list[str]:
    names = categories_service.names(user_id) or [categories_service.fallback_name(user_id)]
    guesses: list[Optional[str]] = [None] * len(titles)
    if len(names) > 1:
        try:
            import groq_client
            guesses = []
            for i in range(0, len(titles), 40):
                guesses += groq_client.classify_event_titles(titles[i:i + 40], names)
        except Exception as exc:
            logger.warning("Категории событий не подобраны: %s", type(exc).__name__)
            guesses = [None] * len(titles)
    return [categories_service.resolve_name(user_id, g or "") or names[0] for g in guesses]


def sync_user(user_id: str, now_utc: Optional[datetime] = None) -> Optional[dict]:
    """Один проход для пользователя. None — календарь не подключён или токен не работает."""
    service = gcal._get_service(user_id)
    if not service:
        return None
    tz = users_service.timezone_of(user_id)
    calendars = gcal.import_calendars(gcal._get_row(user_id))
    events, complete = [], {}  # complete: календарь → прочитан ли целиком
    for cal in calendars:
        try:
            got, complete[cal] = fetch_events(service, tz, now_utc, cal)
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if cal != "primary" and status in (403, 404):
                logger.info("Выбранный календарь недоступен — пропущен")  # удалили или закрыли доступ
                continue
            gcal._note_sync_failure(user_id, exc, "import")
            return None
        except Exception as exc:
            gcal._note_sync_failure(user_id, exc, "import")
            return None
        known = {e["id"] for e in events}
        events += [{**e, "_cal": cal} for e in got if e.get("id") not in known]
    if not complete:
        return None

    linked = _linked_tasks(user_id)
    stats = {"created": 0, "updated": 0, "deleted": 0}
    fresh, seen_ids = [], set()
    for ev in events:
        seen_ids.add(ev["id"])
        task = linked.get(ev["id"])
        if ev.get("status") == "cancelled":
            if task:
                stats["deleted"] += _drop_task(task, user_id) == "deleted"
            continue
        if task:
            stats["updated"] += _apply_event(task, ev, user_id, tz)
            continue
        if ev.get("eventType") in SKIP_TYPES or _declined(ev) or is_julyera_event(ev):
            continue
        deadline, all_day = event_deadline(ev, tz)
        if deadline:
            fresh.append((ev, deadline, all_day))

    if fresh:
        titles = [_title(ev, False) for ev, _, _ in fresh]
        for (ev, deadline, all_day), title, category in zip(fresh, titles, _categories(user_id, titles)):
            row = {"user_id": user_id, "task": title, "category": category, "source": SOURCE,
                   "deadline": deadline.isoformat(), "priority": "Mid", "status": "To Do",
                   "note": _plain(ev.get("description")), "is_recurring": False, "tags": [],
                   "google_event_id": ev["id"], "gcal_imported": True, "gcal_all_day": all_day,
                   "gcal_updated": ev.get("updated")}
            if ev["_cal"] != "primary":  # у задач из основного календаря колонка пустая, как до миграции 008
                row["gcal_calendar_id"] = ev["_cal"]
            try:
                supabase.table(TABLE).insert(row).execute()
                stats["created"] += 1
            except Exception as exc:  # параллельный проход уже создал эту задачу (уникальный индекс)
                logger.info("Событие не перенесено: %s", type(exc).__name__)

    # Незакрытые задачи с событием, которого нет в полном списке своего календаря: спрашиваем событие напрямую.
    # Задачи из календарей, которые сейчас не выбраны или прочитаны не целиком, не трогаем.
    if any(complete.values()):
        today = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        today = today.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = today + timedelta(days=RECURRING_DAYS)
        lookups = 0
        for event_id, task in linked.items():
            cal = task.get("gcal_calendar_id") or "primary"
            if event_id in seen_ids or task.get("status") == "Done" or lookups >= MAX_LOOKUPS or not complete.get(cal):
                continue
            deadline = _naive(task.get("deadline"))
            if not deadline or deadline < today:
                continue  # прошлые события в список не попадают — это не значит, что их удалили
            if deadline >= window_end and "_" in event_id:
                continue  # повторение серии за окном не читается — не повод удалять
            lookups += 1
            if _event_gone(service, event_id, user_id, cal):
                stats["deleted"] += _drop_task(task, user_id) == "deleted"
    if any(stats.values()):
        logger.info("Календарь → задачи: создано %s, изменено %s, удалено %s",
                    stats["created"], stats["updated"], stats["deleted"])
    return stats


def _event_gone(service, event_id: str, user_id: str, calendar_id: str = "primary") -> bool:
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (404, 410):
            return True
        gcal._note_sync_failure(user_id, exc, "lookup")
        return False
    except Exception as exc:
        gcal._note_sync_failure(user_id, exc, "lookup")
        return False
    return (event or {}).get("status") == "cancelled"


def run_all(now_utc: Optional[datetime] = None) -> list[tuple[str, str]]:
    """Проход по всем, у кого подключён календарь. Возвращает сообщения о первом переносе (user_id, текст).
    Сбой у одного пользователя не мешает остальным."""
    if not gcal.is_configured():
        return []
    rows = supabase.table(gcal.CREDENTIALS_TABLE).select("user_id,needs_reconnect,imported_at").execute().data
    switched_off = {u["id"] for u in users_service.list_users() if u.get("calendar_import") is False}
    messages = []
    for row in rows:
        if row.get("needs_reconnect") or row["user_id"] in switched_off:
            continue  # выключатель в меню: ни новых задач, ни правок и удалений из календаря
        try:
            stats = sync_user(row["user_id"], now_utc)
        except Exception as exc:
            logger.warning("Синхронизация календаря не прошла: %s", type(exc).__name__)
            continue
        if stats is not None and not row.get("imported_at"):
            text = _mark_first_import(row["user_id"], stats)
            if text:
                messages.append((row["user_id"], text))
    return messages


def _mark_first_import(user_id: str, stats: dict) -> Optional[str]:
    """Первый проход после подключения: запоминаем его и возвращаем текст «перенёс N событий» (если что-то было)."""
    supabase.table(gcal.CREDENTIALS_TABLE).update({"imported_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("user_id", user_id).execute()
    return FIRST_IMPORT_TEXT.format(n=stats["created"]) if stats["created"] else None


def sync_after_connect(user_id: str) -> None:
    """Сразу после подключения календаря (фоном из /google/callback): перенести события и написать об этом."""
    try:
        row = gcal._get_row(user_id) or {}
        if not users_service.calendar_import_enabled(user_id):
            return
        stats = sync_user(user_id)
        if stats is None or row.get("imported_at"):
            return
        text = _mark_first_import(user_id, stats)
        if text:
            import notifier
            notifier.notify_user(user_id, text, with_app_button=True)
    except Exception as exc:
        logger.warning("Перенос событий после подключения не прошёл: %s", type(exc).__name__)
