-- 014: место у задач и заметок, группы с общими задачами.
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.

-- Место (адрес текстом): у задачи уходит и в событие Google Calendar.
alter table tasks add column if not exists location text;
alter table notes add column if not exists location text;

-- Группы: у каждой своя страница в приложении и ссылка-приглашение (t.me/<бот>?start=g_<invite_token>).
-- tick_all: галочка закрывает задачу у всех (true) или у каждого своя («1 из 3 готово», false).
create table if not exists task_groups (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 30),
  tick_all boolean not null default true,
  invite_token text not null unique,
  created_at timestamptz not null default now()
);
alter table task_groups enable row level security;

create table if not exists task_group_members (
  group_id uuid not null references task_groups(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  joined_at timestamptz not null default now(),
  primary key (group_id, user_id)
);
create index if not exists task_group_members_user_idx on task_group_members (user_id);
alter table task_group_members enable row level security;

-- Задача группы: у каждого участника своя копия, у всех копий shared_id = id главной копии
-- (на ней общие шаги и комментарии), group_id — группа.
alter table tasks add column if not exists shared_id uuid;
create index if not exists tasks_shared_idx on tasks (shared_id) where shared_id is not null;
alter table tasks add column if not exists group_id uuid references task_groups(id) on delete set null;
create index if not exists tasks_group_idx on tasks (group_id) where group_id is not null;
