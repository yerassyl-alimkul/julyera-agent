-- 005: комментарии к задачам (текст, ссылки, фото и PDF). Только добавляет: таблицу task_comments
-- и закрытое хранилище файлов task-files в Supabase Storage. Выполняет владелец в SQL Editor целиком.
begin;

create table task_comments (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references tasks(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  text text check (text is null or char_length(text) <= 2000),
  file_path text,          -- путь в хранилище task-files: <user_id>/<task_id>/<id>.<ext>
  file_name text,
  file_type text,
  file_size int,
  created_at timestamptz not null default now(),
  check (text is not null or file_path is not null)
);
create index task_comments_task_idx on task_comments (task_id, created_at);
create index task_comments_user_idx on task_comments (user_id);
alter table task_comments enable row level security;

-- закрытое хранилище: без политик доступа публичный ключ его не видит; backend ходит ключом service_role,
-- а приложению отдаёт ссылки на файлы, которые живут один час
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('task-files', 'task-files', false, 10485760,
        array['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'application/pdf'])
on conflict (id) do nothing;

commit;
