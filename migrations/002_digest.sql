-- 002: сводки и напоминания с кнопками (шаг 7).
-- Только добавляет: tasks.snoozed_until, users.digest_enabled, таблицу digest_log. Старый код от неё не ломается.
-- Выполняет владелец в Supabase SQL Editor целиком, одним запуском.
begin;

-- «Отложить на час»: до этого момента задача не напоминается
alter table tasks add column snoozed_until timestamptz;

-- /digest off выключает утреннюю и вечернюю сводку
alter table users add column digest_enabled boolean not null default true;

-- что уже отправлено сегодня: строка вставляется ДО отправки, повтор упирается в первичный ключ
create table digest_log (
  user_id uuid not null references users(id) on delete cascade,
  kind text not null check (kind in ('morning','evening','remind_1','remind_2')),
  day date not null,
  sent_at timestamptz not null default now(),
  primary key (user_id, kind, day)
);

-- запрет доступа через публичный API Supabase; backend работает ключом service_role
alter table digest_log enable row level security;

commit;
