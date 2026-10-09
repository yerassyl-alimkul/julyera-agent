-- 009: напоминание перед задачей, «завтра спишется», еженедельная резервная копия (digest_service, backup_service).
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен.

-- За сколько минут до задачи со временем напоминать (0 — не напоминать). Выбор в меню приложения и /remind.
alter table users add column if not exists remind_before int not null default 30;
alter table users drop constraint if exists users_remind_before_check;
alter table users add constraint users_remind_before_check check (remind_before in (0, 15, 30, 60));

-- Одно напоминание на задачу и её срок: перенесли задачу — напомнит снова.
create table if not exists pre_reminder_log (
  task_id uuid not null references tasks(id) on delete cascade,
  deadline timestamptz not null,
  sent_at timestamptz not null default now(),
  primary key (task_id, deadline)
);
alter table pre_reminder_log enable row level security;

-- Новые виды рассылок в журнале: «завтра спишется» и резервная копия.
alter table digest_log drop constraint if exists digest_log_kind_check;
alter table digest_log add constraint digest_log_kind_check
  check (kind in ('morning', 'evening', 'remind_1', 'remind_2', 'charges', 'backup'));
