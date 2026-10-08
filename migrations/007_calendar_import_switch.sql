-- 007: выключатель «Переносить события в задачи» в меню приложения (calendar_sync.py).
-- Выполнить в Supabase SQL Editor. Повторный запуск безопасен. По умолчанию включено у всех.
alter table users add column if not exists calendar_import boolean not null default true;
