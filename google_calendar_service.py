"""
Интеграция с Google Calendar: OAuth-авторизация и синхронизация задач
с событиями календаря (создание/обновление/удаление события при
создании/изменении/закрытии/удалении задачи).

Обратное направление (события из календаря → задачи, правки и удаления в Google) — calendar_sync.py.
События, созданные в Google (tasks.gcal_imported), здесь не перезаписываются целиком: меняются только
название, описание и время (patch), длительность, место и гости остаются как были.

У каждого пользователя свой календарь: токены хранятся в google_credentials по user_id,
и события задачи пишутся только в календарь её владельца.
"""
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import notifier
import users_service
from database import supabase

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")
# Секрет для подписи state при подключении календаря (случайная строка от 32 символов, задаётся на Render).
OAUTH_STATE_SECRET = os.environ.get("OAUTH_STATE_SECRET")
STATE_TTL_SECONDS = 600  # подписанная метка в state живёт 10 минут

# Живая проверка токена в /google/status кэшируется на 10 минут: user_id → (время, токен рабочий?)
STATUS_CACHE_SECONDS = 600
_status_cache: dict[str, tuple[float, bool]] = {}
RECONNECT_MESSAGE = "Google Calendar отключился. Откройте приложение и переподключите его в меню."

# calendar.events — события во всех календарях пользователя; calendarlist.readonly — список его календарей
# для выбора, откуда переносить события (добавлен 9 октября 2026: старым подключениям нужно переподключение,
# до него работает только основной календарь).
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly"]
# Google может вернуть больше прав, чем запрошено (include_granted_scopes) — oauthlib не должен на это падать.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
CREDENTIALS_TABLE = "google_credentials"

CATEGORY_EMOJI = {"Study": "📚", "Work": "💼", "Personal": "🧍", "UniMe": "🎓"}
PRIORITY_EMOJI = {"High": "🔴", "Mid": "🟡", "Low": "🟢"}
# Метка «событие создал JulYera» — calendar_sync такие события не превращает в новые задачи.
JULYERA_MARK = {"private": {"julyera": "1"}}


def _client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)


def _state_signature(payload: str) -> str:
    return hmac.new(OAUTH_STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_state(user_id: str, now: Optional[float] = None) -> str:
    """Подписанная короткоживущая метка «кто подключает календарь»: user_id.срок.подпись.
    Сам ключ доступа в Google не передаётся."""
    if not OAUTH_STATE_SECRET:
        raise RuntimeError("OAUTH_STATE_SECRET не задан")
    expires = int((time.time() if now is None else now) + STATE_TTL_SECONDS)
    payload = f"{user_id}.{expires}"
    return f"{payload}.{_state_signature(payload)}"


def verify_state(state: Optional[str], now: Optional[float] = None) -> Optional[str]:
    """Возвращает user_id из state, если подпись верна и срок не истёк, иначе None."""
    if not OAUTH_STATE_SECRET or not state:
        return None
    try:
        user_id, expires, signature = state.rsplit(".", 2)
        expires_at = int(expires)
    except ValueError:
        return None
    if not hmac.compare_digest(_state_signature(f"{user_id}.{expires}"), signature):
        return None
    if (time.time() if now is None else now) > expires_at:
        return None
    return user_id


def build_auth_url(user_id: str) -> str:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # гарантирует, что Google вернёт refresh_token
        include_granted_scopes="true",
        state=make_state(user_id),
    )
    return auth_url


def exchange_code(code: str, user_id: str) -> None:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    flow.fetch_token(code=code)
    _save_credentials(flow.credentials, user_id, reconnected=True)
    _status_cache[user_id] = (time.time(), True)


def _save_credentials(creds: Credentials, user_id: str, reconnected: bool = False) -> None:
    data = {
        "user_id": user_id,
        "access_token": creds.token,
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
        "updated_at": datetime.utcnow().isoformat(),
    }
    # refresh_token Google отдаёт только при первом согласии (prompt=consent).
    # При последующих обновлениях access_token его может не быть в объекте —
    # тогда не затираем уже сохранённое значение пустым.
    if creds.refresh_token:
        data["refresh_token"] = creds.refresh_token
    if reconnected:
        data["needs_reconnect"] = False
    supabase.table(CREDENTIALS_TABLE).upsert(data).execute()


def _get_row(user_id: str) -> Optional[dict]:
    result = supabase.table(CREDENTIALS_TABLE).select("*").eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def _refresh(creds: Credentials) -> None:
    """Обновление access-токена у Google (отдельная функция, чтобы проверки могли подменить сеть)."""
    creds.refresh(GoogleRequest())


def _mark_needs_reconnect(user_id: str, reason: str) -> None:
    """Токен больше не работает: ставим needs_reconnect и один раз пишем пользователю в Telegram.
    Пока флаг стоит, синхронизация пропускается; снимает его только успешное переподключение."""
    _status_cache[user_id] = (time.time(), False)
    row = _get_row(user_id)
    if not row or row.get("needs_reconnect"):
        return  # уже помечено — сообщение было, повторять не нужно
    supabase.table(CREDENTIALS_TABLE).update({"needs_reconnect": True}).eq("user_id", user_id).execute()
    logger.warning("Google Calendar пользователя требует переподключения: %s", reason)
    notifier.notify_user(user_id, RECONNECT_MESSAGE, with_app_button=True)


def status(user_id: str) -> dict:
    """Честный статус календаря: есть ли запись и работает ли токен на самом деле."""
    result = {"configured": is_configured(), "connected": False, "needs_reconnect": False}
    row = _get_row(user_id)
    if not row:
        return result
    result["connected"] = True
    if row.get("needs_reconnect"):
        result["needs_reconnect"] = True
        return result
    cached = _status_cache.get(user_id)
    if cached and time.time() - cached[0] < STATUS_CACHE_SECONDS:
        result["needs_reconnect"] = not cached[1]
        return result
    if not result["configured"]:
        return result
    try:
        creds = _credentials_from_row(row)
        _refresh(creds)  # живая проверка: Google обменивает refresh_token на свежий access-токен
        _save_credentials(creds, user_id)
        _status_cache[user_id] = (time.time(), True)
    except RefreshError as exc:
        _mark_needs_reconnect(user_id, f"проверка статуса: {type(exc).__name__}")
        result["needs_reconnect"] = True
    except Exception:
        # сеть или Google недоступен — это не повод просить переподключение; не кэшируем
        logger.exception("Не удалось проверить токен Google Calendar")
    return result


def _credentials_from_row(row: dict) -> Credentials:
    # Срок действия access_token обязательно передаём: без него google-auth считает
    # токен вечным, не обновляет его заранее и падает уже посреди запроса к API.
    expiry = None
    if row.get("token_expiry"):
        expiry = datetime.fromisoformat(row["token_expiry"])
        if expiry.tzinfo is not None:  # google-auth ждёт «голое» время в UTC
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token=row.get("access_token"),
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        # scopes не передаём: при обновлении токена Google иначе требует ровно эти права, и старое подключение
        # (без calendarlist) сломалось бы с invalid_scope. Без них токен получает те права, что были выданы.
        expiry=expiry,
    )


def _load_credentials(user_id: str) -> Optional[Credentials]:
    row = _get_row(user_id)
    if not row or row.get("needs_reconnect"):
        return None  # не подключён или токен уже признан нерабочим — синхронизацию пропускаем
    creds = _credentials_from_row(row)
    if not creds.valid:
        try:
            _refresh(creds)
        except RefreshError as exc:
            _mark_needs_reconnect(user_id, f"обновление токена: {type(exc).__name__}")
            return None
        _save_credentials(creds, user_id)
    return creds


def _get_service(user_id: str):
    if not is_configured() or not user_id:
        return None
    try:
        creds = _load_credentials(user_id)
    except Exception:
        logger.exception("Не удалось загрузить/обновить Google-креды")
        return None
    if not creds:
        return None
    try:
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        logger.exception("Не удалось создать клиент Google Calendar")
        return None


def _event_body(task: dict, tz: str = "Europe/Rome") -> dict:
    emoji_c = CATEGORY_EMOJI.get(task.get("category"), "")
    emoji_p = PRIORITY_EMOJI.get(task.get("priority"), "")
    start_dt = datetime.fromisoformat(task["deadline"])
    # В нашей системе дедлайн хранится и везде показывается как "голая" стена
    # часов (12:00 значит 12:00 в поясе пользователя, users.timezone, без UTC-пересчёта), но
    # Supabase иногда возвращает строку с суффиксом +00:00. Google Calendar,
    # в отличие от бота и веб-интерфейса, ЧЕСТНО пересчитывает время по этому
    # смещению — поэтому убираем его и передаём зону отдельным полем timeZone.
    if start_dt.tzinfo is not None:
        start_dt = start_dt.replace(tzinfo=None)
    end_dt = _naive(task["ends_at"]) if task.get("ends_at") else start_dt + timedelta(minutes=30)
    return {
        "summary": f"{emoji_c} {emoji_p} {task['task']}".strip(),
        "description": task.get("note") or "",
        "location": task.get("location") or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        "extendedProperties": JULYERA_MARK,
    }


# id повторения серии у Google: <id серии>_<дата начала>, например abc_20261012T090000Z или abc_20261012 (весь день)
_INSTANCE_ID = re.compile(r"^(.+)_(\d{8}(?:T\d{6}Z?)?)$")


def series_of(event_id: Optional[str]) -> Optional[str]:
    """id серии для повторения повторяющегося события, иначе None."""
    found = _INSTANCE_ID.match(event_id or "")
    return found.group(1) if found else None


def patch_series(user_id: str, calendar_id: Optional[str], series_id: str, fields: dict) -> None:
    """Название и/или заметка, изменённые у одного повторения, — всей серии в Google («все события»).
    Повторения, которые в Google правили отдельно, Google не трогает. Ошибки только логируются."""
    body = {}
    if "task" in fields:
        body["summary"] = fields["task"]
    if "note" in fields:
        body["description"] = fields["note"] or ""
    if not body:
        return
    service = _get_service(user_id)
    if not service:
        return
    cal = calendar_id or "primary"
    try:
        service.events().patch(calendarId=cal, eventId=series_id, sendUpdates="none", body=body).execute()
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) in (403, 404, 410):
            logger.info("Серию в календаре не изменить (только чтение или удалена)")
            return
        _note_sync_failure(user_id, exc, "series")
    except Exception as exc:
        _note_sync_failure(user_id, exc, "series")


def _naive(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _imported_patch(task: dict, event: dict, tz: str) -> dict:
    """Правка события, пришедшего из Google: название, описание и время. Длительность берём у самого события.
    Задача «на весь день» (gcal_all_day) с временем 23:59 остаётся событием на весь день."""
    start = _naive(task["deadline"])
    old_start, old_end = event.get("start") or {}, event.get("end") or {}
    body = {"summary": task["task"], "description": task.get("note") or ""}
    if task.get("location"):  # место из JulYera; пустое не стирает место события
        body["location"] = task["location"]
    if task.get("gcal_all_day") and start.strftime("%H:%M") == "23:59":
        days = 1
        if old_start.get("date") and old_end.get("date"):
            days = max(1, (date.fromisoformat(old_end["date"]) - date.fromisoformat(old_start["date"])).days)
        body["start"] = {"date": start.date().isoformat(), "dateTime": None, "timeZone": None}
        body["end"] = {"date": (start.date() + timedelta(days=days)).isoformat(), "dateTime": None, "timeZone": None}
        return body
    duration = timedelta(minutes=30)
    if task.get("ends_at"):  # «до» задано в JulYera — конец события ровно по нему
        duration = _naive(task["ends_at"]) - start
    elif old_start.get("dateTime") and old_end.get("dateTime"):
        duration = max(timedelta(0), datetime.fromisoformat(old_end["dateTime"]) - datetime.fromisoformat(old_start["dateTime"]))
    body["start"] = {"dateTime": start.isoformat(), "timeZone": tz, "date": None}
    body["end"] = {"dateTime": (start + duration).isoformat(), "timeZone": tz, "date": None}
    return body


def sync_task_upsert_ex(task: dict, user_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Создаёт или обновляет событие в Google Calendar пользователя user_id для его задачи.
    Возвращает (google_event_id, updated события) — updated запоминается в задаче (gcal_updated), чтобы
    calendar_sync отличал наши правки от правок в Google. Если синхронизация невозможна (Google не подключён,
    нет дедлайна и т.п.) — (прежний id или None, None): это не ошибка, задача живёт в нашей базе.
    """
    if not task.get("deadline"):
        return task.get("google_event_id"), None

    service = _get_service(user_id)
    if not service:
        return task.get("google_event_id"), None

    event_id = task.get("google_event_id")

    try:
        tz = users_service.timezone_of(user_id)
        if event_id and task.get("gcal_imported"):
            cal = task.get("gcal_calendar_id") or "primary"
            try:
                event = service.events().get(calendarId=cal, eventId=event_id).execute()
                saved = service.events().patch(calendarId=cal, eventId=event_id, sendUpdates="none",
                                               body=_imported_patch(task, event, tz)).execute()
            except HttpError as exc:
                if cal != "primary" and getattr(getattr(exc, "resp", None), "status", None) == 403:
                    logger.info("Календарь только для чтения — правка задачи в событие не переносится")
                    return event_id, None  # чужой календарь без права записи — это не повод переподключать
                raise
            return event_id, (saved or {}).get("updated")
        body = _event_body(task, tz)
        if event_id:
            saved = service.events().update(calendarId="primary", eventId=event_id, body=body).execute()
            return event_id, (saved or {}).get("updated")
        created = service.events().insert(calendarId="primary", body=body).execute()
        return created["id"], created.get("updated")
    except Exception as exc:
        # Не только HttpError: сеть, тайм-аут, отозванный refresh_token (RefreshError).
        # Задача уже сохранена в базе, поэтому ошибка календаря не должна ронять запрос.
        _note_sync_failure(user_id, exc, "upsert")
        return event_id, None


def sync_task_upsert(task: dict, user_id: str) -> Optional[str]:
    """То же, что sync_task_upsert_ex, но только id события."""
    return sync_task_upsert_ex(task, user_id)[0]


def sync_task_delete(event_id: Optional[str], user_id: str, calendar_id: Optional[str] = None) -> None:
    if not event_id:
        return
    service = _get_service(user_id)
    if not service:
        return
    cal = calendar_id or "primary"
    try:
        # sendUpdates="none": если у события были гости, Google не рассылает им писем об отмене
        service.events().delete(calendarId=cal, eventId=event_id, sendUpdates="none").execute()
    except HttpError as exc:
        if cal != "primary" and getattr(getattr(exc, "resp", None), "status", None) == 403:
            logger.info("Календарь только для чтения — событие не удалено")
            return
        _note_sync_failure(user_id, exc, "delete")
    except Exception as exc:
        _note_sync_failure(user_id, exc, "delete")


class CalendarListUnavailable(Exception):
    """Подключение старое, без права видеть список календарей: нужно переподключить Google Calendar."""


def list_calendars(user_id: str) -> Optional[list[dict]]:
    """Календари пользователя для выбора, откуда переносить события. None — календарь не подключён.
    Основной календарь всегда с id "primary". Календари «только занятость» не показываем."""
    service = _get_service(user_id)
    if not service:
        return None
    items, token = [], None
    try:
        for _ in range(5):
            page = service.calendarList().list(minAccessRole="reader", pageToken=token).execute()
            items.extend(page.get("items") or [])
            token = page.get("nextPageToken")
            if not token:
                break
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) == 403:
            raise CalendarListUnavailable()
        raise
    out = []
    for c in items:
        if c.get("deleted") or c.get("hidden") and not c.get("primary"):
            continue
        out.append({"id": "primary" if c.get("primary") else c["id"],
                    "name": "Основной" if c.get("primary") else (c.get("summaryOverride") or c.get("summary") or "Без названия"),
                    "color": c.get("backgroundColor"), "primary": bool(c.get("primary")),
                    "read_only": c.get("accessRole") not in ("owner", "writer")})
    out.sort(key=lambda c: (not c["primary"], c["name"].lower()))
    return out


def import_calendars(row: Optional[dict]) -> list[str]:
    """Из каких календарей переносить события (google_credentials.import_calendars, миграция 008).
    Пусто — только основной."""
    value = (row or {}).get("import_calendars")
    return list(value) if value else ["primary"]


def set_import_calendars(user_id: str, ids: list[str]) -> list[str]:
    """Сохранить выбор. Только календари из списка пользователя (иначе ValueError). Пустой выбор нельзя:
    чтобы ничего не переносить, есть выключатель."""
    available = {c["id"] for c in (list_calendars(user_id) or [])}
    chosen = list(dict.fromkeys(ids))
    if not chosen or any(i not in available for i in chosen):
        raise ValueError("unknown calendar")
    supabase.table(CREDENTIALS_TABLE).update({"import_calendars": chosen}).eq("user_id", user_id).execute()
    return chosen


def _note_sync_failure(user_id: str, exc: Exception, action: str) -> None:
    """Отказ в доступе (отозванный токен, 401/403 от API) ставит needs_reconnect;
    сеть, 5xx и удалённое вручную событие (404) — временные сбои, их только логируем."""
    status_code = getattr(getattr(exc, "resp", None), "status", None) if isinstance(exc, HttpError) else None
    if isinstance(exc, RefreshError) or status_code in (401, 403):
        _mark_needs_reconnect(user_id, f"синхронизация ({action}): {type(exc).__name__} {status_code or ''}".strip())
    else:
        logger.exception("Ошибка синхронизации с Google Calendar (%s)", action)
