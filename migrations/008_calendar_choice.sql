-- 008: выбор календарей, из которых события переносятся в задачи (calendar_sync.py).
-- Выполнить в Supabase SQL Editor. Повторный запуск безопасен.
-- Какие календари читать ("primary" — основной); пусто — только основной, как раньше.
alter table google_credentials add column if not exists import_calendars text[];
-- В каком календаре событие задачи; пусто — в основном (все задачи до этой миграции).
alter table tasks add column if not exists gcal_calendar_id text;
