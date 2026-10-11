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

-- Групповая задача: у каждого своя галочка («1 из 3 готово»); для двоих — false (галочка закрывает у обоих).
alter table tasks add column if not exists shared_each boolean not null default false;
-- Приглашение от группы: у принявшего задача встаёт в категорию с названием группы.
alter table task_invites add column if not exists group_name text;

-- Контакты: взаимные (строка в обе стороны), появляются только после «Принять» по ссылке.
create table if not exists contacts (
  user_id uuid not null references users(id) on delete cascade,
  contact_id uuid not null references users(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, contact_id)
);
alter table contacts enable row level security;

-- Ссылка-приглашение в контакты (t.me/<бот>?start=c_<token>), живёт 7 дней.
create table if not exists contact_links (
  token text primary key,
  user_id uuid not null references users(id) on delete cascade,
  created_at timestamptz not null default now()
);
alter table contact_links enable row level security;

-- Группы: личный набор своих контактов; задача на двоих и больше ставится только группой.
create table if not exists task_groups (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 30),
  created_at timestamptz not null default now()
);
create index if not exists task_groups_user_idx on task_groups (user_id);
alter table task_groups enable row level security;
create table if not exists task_group_members (
  group_id uuid not null references task_groups(id) on delete cascade,
  member_id uuid not null references users(id) on delete cascade,
  primary key (group_id, member_id)
);
alter table task_group_members enable row level security;
