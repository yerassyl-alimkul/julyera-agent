"""
Инициализация клиента Supabase.
Ключи берутся из переменных окружения (.env) — никогда не хардкодим их в коде.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client


def _force_http1() -> None:
    """Клиенты базы (postgrest) и хранилища (storage3) жёстко включают HTTP/2. Под параллельными запросами из
    потоков FastAPI соединение HTTP/2 иногда рвётся (RemoteProtocolError: ConnectionTerminated) — на проде это
    случайные 500 примерно на 2% запросов (проверено 8–9 октября 2026). HTTP/1.1 у httpx с потоками работает надёжно."""
    try:
        import postgrest._sync.client as pg

        def pg_session(self, base_url, headers, timeout, verify=True):
            return pg.SyncClient(base_url=base_url, headers=headers, timeout=timeout, verify=verify,
                                 follow_redirects=True, http2=False)
        pg.SyncPostgrestClient.create_session = pg_session
    except (ImportError, AttributeError):
        pass
    try:
        import storage3._sync.client as st

        def st_session(self, base_url, headers, timeout, verify=True):
            return st.SyncClient(base_url=base_url, headers=headers, timeout=timeout, verify=bool(verify),
                                 follow_redirects=True, http2=False)
        st.SyncStorageClient._create_session = st_session
    except (ImportError, AttributeError):
        pass


_force_http1()

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL и SUPABASE_KEY должны быть заданы в .env файле. "
        "См. .env.example."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
