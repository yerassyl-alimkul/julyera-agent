-- 013: фото у заметок и повторяющиеся платежи «по галочке» (задачей).
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.

-- Фото у заметок. Файлы — в том же закрытом хранилище task-files: <user_id>/notes/<note_id>/<id>.<ext>.
create table if not exists note_files (
  id uuid primary key default gen_random_uuid(),
  note_id uuid not null references notes(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  file_path text not null,
  file_type text not null,
  file_size int,
  created_at timestamptz not null default now()
);
create index if not exists note_files_note_idx on note_files (note_id, created_at);
create index if not exists note_files_user_idx on note_files (user_id);
alter table note_files enable row level security;

-- Повторяющийся платёж «Списывать после галочки»: вместо автоматической операции — задача «💳 …»;
-- галочка на задаче записывает операцию и ставит задачу на следующий срок.
alter table money_recurring add column if not exists as_task boolean not null default false;
alter table tasks add column if not exists money_recurring_id uuid references money_recurring(id) on delete set null;
-- срок платежа, за который эта задача (дедлайн задачи можно перенести, срок — нет)
alter table tasks add column if not exists money_due date;
-- у платежа не больше одной открытой задачи
create unique index if not exists tasks_money_recurring_open_idx
  on tasks (money_recurring_id) where money_recurring_id is not null and status <> 'Done';
