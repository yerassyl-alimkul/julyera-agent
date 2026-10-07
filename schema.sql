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

-- Заметки: третья страница — свободный текст, не привязан к задачам.
create table notes (
    id uuid primary key default gen_random_uuid(),
    content text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Теги: свободные метки поверх категорий.
alter table tasks add column tags text[] not null default '{}';

-- Подзадачи: чек-лист шагов внутри задачи.
create table subtasks (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references tasks(id) on delete cascade,
    content text not null,
    is_done boolean not null default false,
    created_at timestamptz not null default now()
);

create index idx_subtasks_task_id on subtasks (task_id);

-- Новая категория для трекера заявок на работу.
alter table tasks drop constraint tasks_category_check;
alter table tasks add constraint tasks_category_check
  check (category in ('Study', 'Work', 'Personal', 'UniMe', 'JobSearch'));

-- Откат: JobSearch не нужна (пользователь уже работает, не ищет).
alter table tasks drop constraint tasks_category_check;
alter table tasks add constraint tasks_category_check
  check (category in ('Study', 'Work', 'Personal', 'UniMe'));

-- Несколько пользователей (Yera и допущенные им люди). Перед запуском проверить фактические
-- имена ограничений google_credentials (google_credentials_pkey, singleton_row):
--   select conname from pg_constraint where conrelid = 'google_credentials'::regclass;
-- Все существующие данные закрепляются за Yera. Новых людей создаёт бот после одобрения владельцем.
create table users (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  telegram_user_id bigint unique,
  is_admin boolean not null default false,
  access_key text not null unique
    default replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''),
  created_at timestamptz default now()
);
insert into users (name, is_admin) values ('Yera', true);

-- в личном чате chat_id совпадает с Telegram-id пользователя
update users set telegram_user_id =
  (select chat_id from telegram_chats order by created_at limit 1)
  where name = 'Yera';

alter table tasks add column user_id uuid references users(id);
alter table notes add column user_id uuid references users(id);
alter table telegram_chats add column user_id uuid references users(id);
update tasks set user_id = (select id from users where name = 'Yera');
update notes set user_id = (select id from users where name = 'Yera');
update telegram_chats set user_id = (select id from users where name = 'Yera');
alter table tasks alter column user_id set not null;
alter table notes alter column user_id set not null;
alter table telegram_chats alter column user_id set not null;
create index tasks_user_idx on tasks(user_id);
create index notes_user_idx on notes(user_id);
create index telegram_chats_user_idx on telegram_chats(user_id);

alter table google_credentials drop constraint singleton_row;
alter table google_credentials add column user_id uuid references users(id);
update google_credentials set user_id = (select id from users where name = 'Yera');
alter table google_credentials drop constraint google_credentials_pkey;
alter table google_credentials drop column id;
alter table google_credentials add primary key (user_id);

-- Проверка после запуска: у Yera должен быть заполнен telegram_user_id.
--   select name, telegram_user_id, is_admin from users;
