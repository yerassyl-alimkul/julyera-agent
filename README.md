# Task Agent Backend — шаг 2

FastAPI-сервер с CRUD-эндпоинтами для задач, подключённый к Supabase.
LLM-парсинг (Groq) добавим на шаге 3 — пока задачи создаются в уже
структурированном виде через `POST /tasks`.

## Что внутри
- `main.py` — приложение и эндпоинты
- `models.py` — Pydantic-схемы (категории, приоритеты, статусы)
- `database.py` — подключение к Supabase
- `schema.sql` — SQL для создания таблицы `tasks`
- `requirements.txt` — зависимости
- `.env.example` — шаблон переменных окружения

## Установка и запуск

1. Создай таблицу в Supabase:
   Supabase Dashboard → SQL Editor → вставь содержимое `schema.sql` → Run.

2. Скопируй `.env.example` в `.env` и заполни своими данными из
   Supabase Dashboard → Project Settings → API:
   ```
   cp .env.example .env
   ```
   `SUPABASE_URL` — Project URL
   `SUPABASE_KEY` — используй `service_role` key для бэкенда (не anon key,
   так как это серверная часть без пользовательской аутентификации)

3. Установи зависимости (лучше в виртуальном окружении):
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

4. Запусти сервер:
   ```
   uvicorn main:app --reload
   ```
   Сервер поднимется на http://127.0.0.1:8000

5. Проверь, что всё работает:
   - http://127.0.0.1:8000/docs — интерактивная документация (Swagger UI),
     можно сразу потыкать все эндпоинты руками
   - http://127.0.0.1:8000/health — должен вернуть `{"status": "ok"}`

## Быстрый тест через curl

Создать задачу:
```bash
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Повторить EU AI Act перед экзаменом",
    "category": "UniMe",
    "deadline": "2026-12-08",
    "priority": "High"
  }'
```

Получить список задач:
```bash
curl http://127.0.0.1:8000/tasks
```

Получить только незавершённые High-приоритетные:
```bash
curl "http://127.0.0.1:8000/tasks?status=To%20Do&priority=High"
```

Обновить статус (закрыть задачу):
```bash
curl -X PATCH http://127.0.0.1:8000/tasks/{id} \
  -H "Content-Type: application/json" \
  -d '{"status": "Done"}'
```

## Следующий шаг
Шаг 3 — подключаем Groq API, чтобы `POST /tasks` мог принимать сырой
неструктурированный текст и сам раскладывать его по полям.
