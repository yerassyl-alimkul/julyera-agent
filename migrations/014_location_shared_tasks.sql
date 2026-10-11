-- 014: место у задач и заметок, совместные задачи.
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.

-- Место (адрес текстом): у задачи уходит и в событие Google Calendar.
alter table tasks add column if not exists location text;
alter table notes add column if not exists location text;

-- Совместные задачи: у каждого участника своя копия, у всех копий shared_id = id главной копии
-- (на ней хранятся общие шаги и комментарии).
alter table tasks add column if not exists shared_id uuid;
create index if not exists tasks_shared_idx on tasks (shared_id) where shared_id is not null;

-- Приглашения в задачу: ждёт ответа / принято / отклонено.
create table if not exists task_invites (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references tasks(id) on delete cascade,
  from_user uuid not null references users(id) on delete cascade,
  to_user uuid not null references users(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'accepted', 'declined')),
  created_at timestamptz not null default now(),
  answered_at timestamptz,
  unique (task_id, to_user)
);
create index if not exists task_invites_to_idx on task_invites (to_user, status);
alter table task_invites enable row level security;
