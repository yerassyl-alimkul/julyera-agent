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

-- Свои категории у каждого пользователя (задача №2, шаг 2). Перед запуском:
--   select category, count(*) from tasks group by 1;   -- значения вне четырёх добавить в набор Yera
--   select conname from pg_constraint where conrelid = 'tasks'::regclass and contype = 'c';
begin;

create table categories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  name text not null,
  color text not null,
  sort_order int not null default 0,
  created_at timestamptz default now()
);
create unique index categories_user_name_idx on categories (user_id, lower(name));
-- Backend ходит с ключом service_role (RLS его не касается); публичный ключ доступа к таблице не получает.
alter table categories enable row level security;

alter table tasks drop constraint tasks_category_check;

insert into categories (user_id, name, color, sort_order)
select u.id, c.name, c.color, c.ord
from users u,
     (values ('Study','#85AACB',0), ('Work','#E2D47E',1),
             ('Personal','#A9C89D',2), ('UniMe','#DCE5FB',3)) as c(name,color,ord)
where u.name = 'Yera';

-- каждому, у кого ещё нет категорий (жена), одна «Общее»
insert into categories (user_id, name, color, sort_order)
select u.id, 'Общее', '#A9C89D', 0
from users u
where not exists (select 1 from categories c where c.user_id = u.id);

-- задачи с категорией, которой нет в списке пользователя, получают его первую
update tasks t set category =
  (select c.name from categories c where c.user_id = t.user_id
   order by c.sort_order limit 1)
where not exists (
  select 1 from categories c where c.user_id = t.user_id and c.name = t.category);

create function rename_category(p_user uuid, p_id uuid, p_name text) returns void
language plpgsql as $$
declare old_name text;
begin
  select name into old_name from categories where id = p_id and user_id = p_user;
  if old_name is null then raise exception 'not_found'; end if;
  update tasks set category = p_name where user_id = p_user and category = old_name;
  update categories set name = p_name where id = p_id and user_id = p_user;
end $$;
-- Вызывать функцию может только сервер (service_role), а не любой с публичным ключом проекта.
revoke execute on function rename_category(uuid, uuid, text) from public, anon, authenticated;
grant execute on function rename_category(uuid, uuid, text) to service_role;

commit;

-- Часовой пояс пользователя и честный статус Google Calendar (задача №2, шаг 3).
alter table users add column timezone text not null default 'Europe/Rome';
alter table google_credentials add column needs_reconnect boolean not null default false;

-- ===== Миграция 001: трекер расходов и доходов (migrations/001_money.sql) =====
-- 001: трекер расходов и доходов (задача №3).
-- Только добавляет: колонку users.default_currency и три таблицы money_*. Старый код от неё не ломается.
-- Выполняет владелец в Supabase SQL Editor целиком, одним запуском.
begin;

alter table users add column default_currency text not null default 'EUR'
  check (default_currency in ('EUR','KZT'));

create table money_categories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  kind text not null check (kind in ('expense','income')),
  name text not null,
  color text not null,
  sort_order int not null default 0,
  created_at timestamptz default now()
);
create unique index money_categories_user_kind_name_idx
  on money_categories (user_id, kind, lower(name));

create table money_recurring (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  kind text not null check (kind in ('expense','income')),
  amount numeric(14,2) not null check (amount > 0 and amount < 1000000000000),
  currency text not null check (currency in ('EUR','KZT')),
  category_id uuid not null references money_categories(id),
  note text,
  day_of_month int not null check (day_of_month between 1 and 31),
  next_run date not null,
  active boolean not null default true,
  created_at timestamptz default now()
);
create index money_recurring_user_idx on money_recurring (user_id);

create table money_transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  kind text not null check (kind in ('expense','income')),
  amount numeric(14,2) not null check (amount > 0 and amount < 1000000000000),
  currency text not null check (currency in ('EUR','KZT')),
  category_id uuid not null references money_categories(id),
  note text,
  spent_on date not null,
  source text not null default 'app'
    check (source in ('app','text','voice','recurring')),
  recurring_id uuid references money_recurring(id) on delete set null,
  created_at timestamptz default now()
);
create index money_transactions_user_date_idx
  on money_transactions (user_id, spent_on desc);
-- один повторяющийся платёж не может создать две операции на одну дату
create unique index money_transactions_recurring_once_idx
  on money_transactions (recurring_id, spent_on) where recurring_id is not null;

-- запрет доступа через публичный API Supabase; backend работает ключом service_role и RLS не замечает
alter table money_categories enable row level security;
alter table money_recurring enable row level security;
alter table money_transactions enable row level security;

-- стартовый набор категорий каждому существующему пользователю (цвета из палитры categories_service.PALETTE)
insert into money_categories (user_id, kind, name, color, sort_order)
select u.id, c.kind, c.name, c.color, c.ord
from users u,
  (values
    ('expense','Еда','#A9C89D',0),
    ('expense','Транспорт','#85AACB',1),
    ('expense','Жильё','#E2D47E',2),
    ('expense','Здоровье','#DCE5FB',3),
    ('expense','Развлечения','#F2B8A2',4),
    ('expense','Подписки','#D9C2EC',5),
    ('expense','Одежда','#F4C8D8',6),
    ('expense','Прочее','#B9E0DA',7),
    ('income','Зарплата','#A9C89D',0),
    ('income','Прочее','#B9E0DA',1)
  ) as c(kind, name, color, ord);

commit;

-- ===== Миграция 002: сводки и напоминания с кнопками (migrations/002_digest.sql) =====
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

-- ===== Миграция 003: кошелёк, Apple Pay, заметки о релизах (migrations/003_wallet_applepay.sql) =====
-- 003: кошелёк (частота повторяющихся платежей), Apple Pay (память магазинов, источник applepay),
-- заметки о релизах в боте. Выполняет владелец в Supabase SQL Editor целиком, одним запуском.
-- Всё добавляется; единственное изменение существующего — CHECK источника операции расширяется
-- значением 'applepay' (старые значения остаются допустимыми; согласовано с владельцем 8 октября 2026).
begin;

-- частота повтора: неделя / 2 недели / месяц / год; anchor_date — дата первой операции правила
alter table money_recurring add column frequency text not null default 'monthly'
  check (frequency in ('weekly','biweekly','monthly','yearly'));
alter table money_recurring add column anchor_date date;

-- Apple Pay: какой магазин в какую категорию (учится на исправлениях пользователя)
create table money_merchant_rules (
  user_id uuid not null references users(id) on delete cascade,
  merchant_key text not null,
  category_id uuid not null references money_categories(id) on delete cascade,
  updated_at timestamptz not null default now(),
  primary key (user_id, merchant_key)
);
alter table money_merchant_rules enable row level security;

alter table money_transactions drop constraint money_transactions_source_check;
alter table money_transactions add constraint money_transactions_source_check
  check (source in ('app','text','voice','recurring','applepay'));

-- какие заметки о релизе бот уже отправил владельцу (вставка до отправки, повтор упирается в ключ)
create table release_log (
  id text primary key,
  sent_at timestamptz not null default now()
);
alter table release_log enable row level security;

commit;

-- ===== Миграция 004: лимиты и месячный отчёт (migrations/004_limits_reports.sql) =====
-- 004: лимиты по категориям расходов и месячный отчёт. Только добавляет: две колонки у money_categories
-- и две таблицы-журнала. Выполняет владелец в Supabase SQL Editor целиком, одним запуском.
begin;

-- месячный лимит категории расходов и валюта, в которой он задан (основная валюта пользователя на момент задания)
alter table money_categories add column monthly_limit numeric(14,2)
  check (monthly_limit is null or (monthly_limit > 0 and monthly_limit < 1000000000000));
alter table money_categories add column limit_currency text
  check (limit_currency is null or limit_currency in ('EUR','KZT'));

-- предупреждения о лимите: каждое (80% и 100%) — один раз на категорию в месяц
create table money_limit_alerts (
  user_id uuid not null references users(id) on delete cascade,
  category_id uuid not null references money_categories(id) on delete cascade,
  month text not null,
  threshold int not null check (threshold in (80, 100)),
  sent_at timestamptz not null default now(),
  primary key (user_id, category_id, month, threshold)
);
alter table money_limit_alerts enable row level security;

-- месячный отчёт 1-го числа: один раз на пользователя за месяц
create table money_report_log (
  user_id uuid not null references users(id) on delete cascade,
  month text not null,
  sent_at timestamptz not null default now(),
  primary key (user_id, month)
);
alter table money_report_log enable row level security;

commit;

-- ===== Миграция 005: комментарии к задачам (migrations/005_task_comments.sql) =====
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

-- ---------------------------------------------------------------- 006 (migrations/006_calendar_import.sql)

-- Задача пришла из календаря (событие создано в Google, а не JulYera): его не перезаписываем целиком
-- и не удаляем из календаря, когда задачу закрывают.
alter table tasks add column if not exists gcal_imported boolean not null default false;
-- Событие «на весь день»: задача стоит на 23:59 этого дня, в календаре остаётся событием на весь день.
alter table tasks add column if not exists gcal_all_day boolean not null default false;
-- Когда событие последний раз менялось (поле updated из Google) на момент нашей синхронизации:
-- если в Google оно новее — значит, событие правили в календаре, переносим правку в задачу.
alter table tasks add column if not exists gcal_updated timestamptz;

-- Когда прошёл первый перенос событий (чтобы сообщение «перенёс N событий» пришло один раз).
alter table google_credentials add column if not exists imported_at timestamptz;

-- Одно событие — одна задача у пользователя (защита от двойного переноса при параллельных запусках).
-- Если эта строка выдаст ошибку «could not create unique index», напишите — значит, уже есть дубли.
create unique index if not exists tasks_user_google_event_uniq
    on tasks (user_id, google_event_id) where google_event_id is not null;

-- ---------------------------------------------------------------- 007 (migrations/007_calendar_import_switch.sql)
alter table users add column if not exists calendar_import boolean not null default true;
