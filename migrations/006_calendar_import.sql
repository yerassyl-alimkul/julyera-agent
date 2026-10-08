-- 006: события Google Calendar → задачи (calendar_sync.py). Только добавления, данные не меняются.
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен.

-- Задача пришла из календаря (событие создано в Google, а не JulYera): его не перезаписываем целиком
-- и не удаляем из календаря, когда задачу закрывают.
alter table tasks add column if not exists gcal_imported boolean not null default false;
-- Событие «на весь день»: задача стоит на 23:59 этого дня, в календаре остаётся событием на весь день.
alter table tasks add column if not exists gcal_all_day boolean not null default false;
-- Когда событие последний раз менялось (поле updated из Google) на момент нашей синхронизации:
-- если в Google оно новее — значит, событие правили в календаре, переносим правку в задачу.
alter table tasks add column if not exists gcal_updated timestamptz;

-- Когда прошёл первый перенос событий (чтобы сообщение «перенёс N событий» пришло один раз).
alter table google_credentials add column if not exists imported_at timestamptz;

-- Одно событие — одна задача у пользователя (защита от двойного переноса при параллельных запусках).
-- Если эта строка выдаст ошибку «could not create unique index», напишите — значит, уже есть дубли.
create unique index if not exists tasks_user_google_event_uniq
    on tasks (user_id, google_event_id) where google_event_id is not null;
