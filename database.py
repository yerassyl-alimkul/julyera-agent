"""
Инициализация клиента Supabase.
Ключи берутся из переменных окружения (.env) — никогда не хардкодим их в коде.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL и SUPABASE_KEY должны быть заданы в .env файле. "
        "См. .env.example."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
