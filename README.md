# JulYera Agent

Личный ИИ-агент для задач на несколько человек: пишешь или наговариваешь задачу, Groq превращает текст
в структурированную задачу, она сохраняется в Supabase и видна в Telegram-боте, веб-приложении (оно же Telegram
Mini App) и Google Calendar. У каждого пользователя свои задачи, заметки, категории и календарь.

Прод: https://julyera-agent.onrender.com (Render, автодеплой из `main`).

Подробное описание устройства, договорённостей и способов проверки — в [CLAUDE.md](CLAUDE.md).

## Стек

Python 3.12, FastAPI, Supabase (`supabase-py`), Groq, python-telegram-bot (webhook), Google Calendar API,
фронтенд — один файл `web/index.html` без сборки. Напоминания запускает GitHub Actions.

## Локальный запуск

1. Скопируй `.env.example` в `.env` и заполни значения (сам `.env` в репозиторий не попадает):
   ```
   cp .env.example .env
   ```
2. Установи зависимости:
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Запусти сервер:
   ```
   uvicorn main:app --reload
   ```
   Без `WEBHOOK_BASE_URL` webhook бота не ставится. `bot.py` (polling) локально не запускай, пока работает прод:
   он снимет webhook у прод-бота.

Интерфейс без настоящей базы: `python design/mock_server.py` → http://localhost:8001.

## База данных

Миграции выполняются вручную в Supabase SQL Editor. Журнал — `schema.sql`; новые миграции (начиная
с трекера расходов) дополнительно кладутся отдельными файлами в `migrations/NNN_name.sql`.

## Проверки

- `python scripts/isolation_check.py` — изоляция данных между пользователями (база в памяти).
- `/health` — жив ли сервис и есть ли связь с базой (`"database": "ok"`).
