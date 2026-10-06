# JulYera Agent: контекст проекта

Личный ИИ-агент для управления задачами. Один владелец, один общий набор данных, авторизации нет.
Пользователь пишет или наговаривает задачу, Groq превращает текст в структурированную задачу,
она сохраняется в Supabase и показывается в Telegram-боте, веб-интерфейсе и Google Calendar.

Файл написан по памяти из длинного чата и мог устареть. Если он расходится с кодом, верь коду
и поправь этот файл.

## Как работать с владельцем

- Отвечай по-русски.
- Владелец хорошо понимает продукт, но терминал, git и инфраструктуру знает слабо.
  Перед командами коротко объясняй, что они делают, и не склеивай несколько команд в одну строку.
- Каждый `git push` в `main` сразу деплоится в прод (Render). Перед пушем спрашивай подтверждение.
- Никогда не выводи и не коммить значения ключей и содержимое `.env`.
  Если секрет попал в чат или лог, скажи, что его надо перевыпустить.

## Стек

- Backend: Python 3.12, FastAPI + uvicorn (`main.py`).
- База: Supabase (Postgres) через `supabase-py` с ключом `service_role` (RLS обходится).
- LLM: Groq. Разбор текста в задачи (модель задана константой `MODEL` в `groq_client.py`),
  голосовые сообщения распознаёт `whisper-large-v3`.
- Клиенты: Telegram-бот, веб-интерфейс `web/index.html` (чистый JS, без сборки).
- Хостинг: Render, бесплатный Web Service, адрес `https://julyera-agent.onrender.com`
  (он же зашит в `reminders.yml` и как запасной `BASE_URL` в `web/index.html`).
  После простоя засыпает, холодный старт до ~50 секунд.
  Версия Python закреплена переменной `PYTHON_VERSION=3.12.7` в Render (`runtime.txt` Render игнорирует, поэтому его в репозитории нет).
- Расписание: GitHub Actions (`.github/workflows/reminders.yml`).
- Календарь: Google Calendar API (OAuth, scope `calendar.events`).

## Структура

- `main.py`: все HTTP-маршруты, webhook Telegram, раздача статики.
- `task_service.py`: логика задач (создание, обновление, повторяющиеся, напоминания, синхронизация с календарём).
- `notes_service.py`, `subtasks_service.py`: CRUD заметок и подзадач.
- `google_calendar_service.py`: OAuth и события календаря.
- `groq_client.py`: промпт `SYSTEM_PROMPT`, разбор текста, распознавание голоса.
- `telegram_handlers.py`: обработчики бота (`/start`, `/list`, текст, голос), общие для polling и webhook.
- `bot.py`: локальный запуск бота в режиме polling.
- `models.py`: Pydantic-модели. `database.py`: клиент Supabase.
- `schema.sql`: журнал миграций (не скрипт для повторного запуска).
- `web/index.html`: весь фронтенд в одном файле.

Маршруты: `/tasks` (+ `/tasks/parse`, `/tasks/{id}`), `/notes`, `/subtasks`, `/tasks/{id}/subtasks`,
`/google/auth|callback|status`, `/telegram/webhook/{token}`, `/internal/reminders/run`, `/health`.

## Переменные окружения (только имена)

`SUPABASE_URL`, `SUPABASE_KEY` (service_role), `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`WEBHOOK_BASE_URL` (только прод), `REMINDERS_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_REDIRECT_URI`, `PYTHON_VERSION` (только Render).
Шаблон с комментариями: `.env.example`.
В GitHub Actions секрет называется `REMINDER_SECRET` (без S) и должен совпадать с `REMINDERS_SECRET` на Render.

## База данных

- `tasks`: `task`, `category` (Study | Work | Personal | UniMe), `source`, `deadline` (timestamptz), `priority` (High | Mid | Low),
  `status` (To Do | In Progress | Done), `note`, `is_recurring`, `recurrence_pattern`, `tags` (text[]), `google_event_id`.
- `subtasks` (связь с `tasks` каскадом), `notes`, `telegram_chats` (для напоминаний),
  `google_credentials` (одна строка, `id = 1`).
- Миграции выполняются вручную в Supabase SQL Editor. Перед новой миграцией спроси владельца, что уже применено.
- В интерфейсе статус `In Progress` не используется: все не-Done задачи показываются вместе как «To Do», Done уходит в «Архив».

## Договорённости, которые нельзя ломать

1. **Время дедлайна хранится «голыми часами» без часового пояса.** Supabase отдаёт его с суффиксом `+00:00`,
   но это ложная метка. В Python перед передачей наружу снимай tzinfo (`replace(tzinfo=None)`; сейчас это делает только
   `_event_body` в `google_calendar_service.py`, остальной код берёт `.date()`/`strftime` и от метки не зависит),
   во фронтенде разбирай строку через `parseNaiveDeadline`. Не используй `new Date(isoString)` для показа времени,
   иначе время сдвинется на часовой пояс браузера.
2. **Часовой пояс событий Google Calendar** сейчас `Europe/Rome` (владелец в Италии). Родной часовой пояс владельца Asia/Almaty.
   Расписание напоминаний посчитано по Алматы: cron 04:00 / 11:00 / 16:00 UTC = 09:00 / 16:00 / 21:00.
3. **Статика монтируется на `/` самой последней** в `main.py`, иначе она перекроет API-маршруты.
4. **Polling и webhook у одного бота несовместимы.** Запуск `bot.py` локально снимает webhook у прод-бота.
   Пока работает прод, локально бота не запускай. Webhook возвращается при перезапуске сервиса на Render.
5. **Имена моделей Groq меняются.** Если Groq вернёт `model_not_found`, смени `MODEL` в `groq_client.py`.
6. Повторяющаяся задача при переводе в Done создаёт следующую (+1 день, +1 неделя или +30 дней).
   Копия переносит теги и сразу получает своё событие в Google Calendar (`update_task` в `task_service.py`).
   Подзадачи не копируются.
7. Синхронизация с Google Calendar односторонняя (задачи → события). Событие удаляется при Done,
   при удалении задачи и при снятии дедлайна. Ошибки календаря логируются и не ломают работу с задачами.
   `PATCH /tasks/{id}` обновляет только переданные поля; явный `null` принимается лишь для
   `deadline`, `note`, `source`, `recurrence_pattern` (`NULLABLE_FIELDS` в `task_service.py`), так снимается дедлайн.
8. Теги берутся из текста только как явные хэштеги, остальное модель выдумывать не должна.
9. Напоминания (`get_reminder_tasks`) берут не-Done задачи с дедлайном до «сегодня + 2 дня» включительно,
   сравнивая только даты. «Сегодня» считается по часам сервера Render (UTC), а не по Алматы.
   Telegram-команда `/list` показывает только статус `To Do`, без `In Progress`.

## Как проверять изменения (автотестов нет)

- Python: импорт и запуск через `fastapi.testclient.TestClient` с фейковыми переменными окружения.
  Обязательно задай фейковые `SUPABASE_URL`, `SUPABASE_KEY` и `GROQ_API_KEY`: без них `database.py`
  и `groq_client.py` падают ещё при импорте. `SUPABASE_KEY` должен быть похож на JWT (например `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmb28iOiJiYXIifQ.abc`),
  иначе клиент Supabase откажется стартовать. `TELEGRAM_BOT_TOKEN` в тесте не задавай:
  при старте приложение пойдёт в сеть к Telegram. Маршруты с обращением к базе при фейковом URL падают, это нормально.
- JS: вытащи `<script>` из `web/index.html` и проверь `node --check`. На Mac владельца `node` нет:
  тогда проверяй через `osascript -l JavaScript` (`new Function(src)` выдаёт синтаксические ошибки).
- В локальном `venv` нет библиотек Google, поэтому `import main` там падает. Логику `task_service`
  проверяй на заглушках `google_calendar_service` и `supabase`.
- Интерфейс без настоящей базы: `venv/bin/python design/mock_server.py` поднимает `http://localhost:8001`
  с сайтом и подставными данными в памяти. Запускай его через Bash в фоне, а во встроенном браузере открывай
  конфигурацию `mock` из `.claude/launch.json` (она только подключается к адресу). Сам браузер запустить
  сервер из папки «Загрузки» не может: процесс зависает без доступа к файлам.
- Для интерфейса важны телефон (боковое меню на экранах уже 880 px) и кириллица.

## Статус и планы

Сделано: задачи с датой и временем, теги, подзадачи, заметки, календарь внутри веба, Undo удаления (5 секунд),
голосовой ввод (Telegram через Whisper, веб через Web Speech API), экспорт CSV и печать в PDF,
напоминания 3 раза в день, Google Calendar, боковое меню на телефоне.

В очереди:
- Утренняя и вечерняя сводка (например команда `/today` и рассылка по расписанию).
- iOS-виджет: Swift-файлы (`TaskModels`, `TaskAPI`, `TaskAgentWidget`, `TaskAgentApp`, `ContentView`) лежат вне репозитория,
  в Xcode не собирались и компилятором не проверялись. Цель iOS 17+.
- Новый мобильный дизайн: идёт в ветке `redesign`. Спецификация, макеты и решения владельца лежат в `design/`
  (`design/DESIGN.md`, раздел 12). Порядок шагов: раздел 11.
- Разбор целого силлабуса на задачи: идея пока не оформлена.

Не делаем: многопользовательский режим (нужны авторизация, `user_id` во всех таблицах, RLS, привязка Telegram).
Двусторонняя синхронизация календаря тоже не реализована.

## Открытые вопросы и уборка

- Проверить в Supabase, что ограничение `tasks_category_check` содержит ровно четыре категории
  (категорию JobSearch добавляли и откатывали, неизвестно, применён ли откат).
- Ключи, попадавшие в переписку (service_role Supabase, токен Telegram-бота), перевыпустить, если ещё не сделано.
- `README.md` устарел (описывает только второй шаг: CRUD без Groq).
- Устаревшие комментарии в коде: docstring `main.py` перечисляет только маршруты задач;
  комментарий в `_event_body` (`google_calendar_service.py`) говорит про Алматы, хотя зона событий `Europe/Rome`.