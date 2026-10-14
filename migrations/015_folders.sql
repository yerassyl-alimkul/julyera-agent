-- 015: папки задач — личные и папки групп (решение владельца 14 октября 2026).
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.

-- Папка либо личная (user_id), либо группы (group_id) — ровно одно из двух.
create table if not exists task_folders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  group_id uuid references task_groups(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 40),
  color text,
  created_by uuid references users(id) on delete set null,
  created_at timestamptz not null default now(),
  check ((user_id is null) <> (group_id is null))
);
create index if not exists task_folders_user_idx on task_folders (user_id);
create index if not exists task_folders_group_idx on task_folders (group_id);
alter table task_folders enable row level security;

-- Задача в папке; удалили папку — задачи остаются без папки.
alter table tasks add column if not exists folder_id uuid references task_folders(id) on delete set null;
create index if not exists tasks_folder_idx on tasks (folder_id) where folder_id is not null;
