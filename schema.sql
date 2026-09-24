-- Вставить в Supabase SQL Editor и выполнить один раз.

create table tasks (
    id uuid primary key default gen_random_uuid(),
    task text not null,
    category text not null check (category in ('Study', 'Work', 'Personal', 'UniMe')),
    source text,
    deadline date,
    priority text not null default 'Mid' check (priority in ('High', 'Mid', 'Low')),
    status text not null default 'To Do' check (status in ('To Do', 'In Progress', 'Done')),
    note text,
    is_recurring boolean not null default false,
    recurrence_pattern text check (recurrence_pattern in ('daily', 'weekly', 'monthly') or recurrence_pattern is null),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index idx_tasks_status_deadline on tasks (status, deadline);
create index idx_tasks_recurring on tasks (is_recurring) where is_recurring = true;

-- Хранит chat_id для отправки проактивных напоминаний в Telegram.
create table telegram_chats (
    chat_id bigint primary key,
    created_at timestamptz not null default now()
);

-- Миграция: дедлайн теперь хранит дату И время, а не только дату.
-- Выполнить один раз в Supabase SQL Editor.
alter table tasks
  alter column deadline type timestamptz using deadline::timestamptz;

-- Google Calendar: хранит OAuth-токены (личный проект — одна строка на аккаунт).
create table google_credentials (
    id int primary key default 1,
    refresh_token text not null,
    access_token text,
    token_expiry timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint singleton_row check (id = 1)
);

-- Связь задачи с созданным для неё событием в Google Calendar.
alter table tasks add column google_event_id text;
