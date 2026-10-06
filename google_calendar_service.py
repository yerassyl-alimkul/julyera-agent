"""
Интеграция с Google Calendar: OAuth-авторизация и синхронизация задач
с событиями календаря (создание/обновление/удаление события при
создании/изменении/закрытии/удалении задачи).

Двусторонний импорт изменений ИЗ Google Calendar обратно в задачи —
отдельный следующий шаг, здесь его пока нет.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from database import supabase

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CREDENTIALS_TABLE = "google_credentials"

CATEGORY_EMOJI = {"Study": "📚", "Work": "💼", "Personal": "🧍", "UniMe": "🎓"}
PRIORITY_EMOJI = {"High": "🔴", "Mid": "🟡", "Low": "🟢"}


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


def build_auth_url() -> str:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # гарантирует, что Google вернёт refresh_token
        include_granted_scopes="true",
    )
    return auth_url


def exchange_code(code: str) -> None:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    flow.fetch_token(code=code)
    _save_credentials(flow.credentials)


def _save_credentials(creds: Credentials) -> None:
    data = {
        "id": 1,
        "access_token": creds.token,
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
        "updated_at": datetime.utcnow().isoformat(),
    }
    # refresh_token Google отдаёт только при первом согласии (prompt=consent).
    # При последующих обновлениях access_token его может не быть в объекте —
    # тогда не затираем уже сохранённое значение пустым.
    if creds.refresh_token:
        data["refresh_token"] = creds.refresh_token
    supabase.table(CREDENTIALS_TABLE).upsert(data).execute()


def is_connected() -> bool:
    result = supabase.table(CREDENTIALS_TABLE).select("id").eq("id", 1).execute()
    return bool(result.data)


def _load_credentials() -> Optional[Credentials]:
    result = supabase.table(CREDENTIALS_TABLE).select("*").eq("id", 1).execute()
    if not result.data:
        return None
    row = result.data[0]
    # Срок действия access_token обязательно передаём: без него google-auth считает
    # токен вечным, не обновляет его заранее и падает уже посреди запроса к API.
    expiry = None
    if row.get("token_expiry"):
        expiry = datetime.fromisoformat(row["token_expiry"])
        if expiry.tzinfo is not None:  # google-auth ждёт «голое» время в UTC
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    creds = Credentials(
        token=row.get("access_token"),
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
        expiry=expiry,
    )
    if not creds.valid:
        creds.refresh(GoogleRequest())
        _save_credentials(creds)
    return creds


def _get_service():
    if not is_configured():
        return None
    try:
        creds = _load_credentials()
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


def _event_body(task: dict) -> dict:
    emoji_c = CATEGORY_EMOJI.get(task.get("category"), "")
    emoji_p = PRIORITY_EMOJI.get(task.get("priority"), "")
    start_dt = datetime.fromisoformat(task["deadline"])
    # В нашей системе дедлайн хранится и везде показывается как "голая" стена
    # часов (12:00 значит 12:00 по Алматы, без реального UTC-пересчёта), но
    # Supabase иногда возвращает строку с суффиксом +00:00. Google Calendar,
    # в отличие от бота и веб-интерфейса, ЧЕСТНО пересчитывает время по этому
    # смещению — поэтому убираем его и передаём зону отдельным полем timeZone.
    if start_dt.tzinfo is not None:
        start_dt = start_dt.replace(tzinfo=None)
    end_dt = start_dt + timedelta(minutes=30)
    return {
        "summary": f"{emoji_c} {emoji_p} {task['task']}".strip(),
        "description": task.get("note") or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Rome"},
    }


def sync_task_upsert(task: dict) -> Optional[str]:
    """
    Создаёт или обновляет событие в Google Calendar для задачи.
    Возвращает актуальный google_event_id, либо прежний/None, если
    синхронизация невозможна (Google не подключён, нет дедлайна и т.п.) —
    это не считается ошибкой, задача всё равно продолжает жить в нашей базе.
    """
    if not task.get("deadline"):
        return task.get("google_event_id")

    service = _get_service()
    if not service:
        return task.get("google_event_id")

    event_id = task.get("google_event_id")

    try:
        body = _event_body(task)
        if event_id:
            service.events().update(calendarId="primary", eventId=event_id, body=body).execute()
            return event_id
        created = service.events().insert(calendarId="primary", body=body).execute()
        return created["id"]
    except Exception:
        # Не только HttpError: сеть, тайм-аут, отозванный refresh_token (RefreshError).
        # Задача уже сохранена в базе, поэтому ошибка календаря не должна ронять запрос.
        logger.exception("Ошибка синхронизации с Google Calendar (upsert)")
        return event_id


def sync_task_delete(event_id: Optional[str]) -> None:
    if not event_id:
        return
    service = _get_service()
    if not service:
        return
    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except Exception:
        logger.exception("Ошибка синхронизации с Google Calendar (delete)")
