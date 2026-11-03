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

-- ---------------------------------------------------------------- 008 (migrations/008_calendar_choice.sql)
-- Какие календари читать ("primary" — основной); пусто — только основной, как раньше.
alter table google_credentials add column if not exists import_calendars text[];
-- В каком календаре событие задачи; пусто — в основном (все задачи до этой миграции).
alter table tasks add column if not exists gcal_calendar_id text;

-- ---------------------------------------------------------------- 009 (migrations/009_reminders_weekly_backup.sql)

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

-- ---------------------------------------------------------------- 010 (migrations/010_precise_repeats.sql)

-- Дни недели для «по дням недели» (0 = пн … 6 = вс) или число месяца для «каждый месяц» / «каждый год».
alter table tasks add column if not exists recurrence_days smallint[];

-- Старое ограничение разрешало только daily / weekly / monthly — заменяем на расширенное.
do $$
declare c text;
begin
  for c in select conname from pg_constraint
           where conrelid = 'tasks'::regclass and contype = 'c'
             and pg_get_constraintdef(oid) ilike '%recurrence_pattern%'
  loop
    execute format('alter table tasks drop constraint %I', c);
  end loop;
end $$;
alter table tasks add constraint tasks_recurrence_pattern_check check (
  recurrence_pattern is null
  or recurrence_pattern in ('daily', 'weekdays', 'weekly', 'days', 'monthly', 'yearly'));

-- ---------------------------------------------------------------- 011 (migrations/011_savings_goals.sql)
create table if not exists money_goals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 40),
  target numeric(14,2) not null check (target > 0 and target < 1000000000000),
  currency text not null check (currency in ('EUR', 'KZT')),
  due_date date,
  achieved_at timestamptz,          -- когда цель достигнута (поздравление один раз)
  created_at timestamptz not null default now()
);
create index if not exists money_goals_user_idx on money_goals (user_id);

-- «Отложить» (+) и «Снять» (−) в валюте цели; сколько отложено — сумма движений.
create table if not exists money_goal_moves (
  id uuid primary key default gen_random_uuid(),
  goal_id uuid not null references money_goals(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  amount numeric(14,2) not null check (amount <> 0 and abs(amount) < 1000000000000),
  created_at timestamptz not null default now()
);
create index if not exists money_goal_moves_goal_idx on money_goal_moves (goal_id);
create index if not exists money_goal_moves_user_idx on money_goal_moves (user_id);

-- запрет доступа через публичный API Supabase; backend работает ключом service_role
alter table money_goals enable row level security;
alter table money_goal_moves enable row level security;

-- ---------------------------------------------------------------- 012 (migrations/012_time_range_currencies.sql)

-- «до» у задачи (конец; deadline — начало). «Голые часы», как deadline.
alter table tasks add column if not exists ends_at timestamptz;

-- Дополнительная валюта пользователя (вторая кнопка в форме траты и целях).
alter table users add column if not exists second_currency text;

-- Валюты: было только EUR и KZT — снимаем старые ограничения и ставим новые на пять валют.
do $$
declare r record;
begin
  for r in select conrelid::regclass::text as tbl, conname from pg_constraint
           where contype = 'c'
             and conrelid in ('users'::regclass, 'money_transactions'::regclass, 'money_recurring'::regclass,
                              'money_categories'::regclass, 'money_goals'::regclass)
             and pg_get_constraintdef(oid) ilike '%currency%'
  loop
    execute format('alter table %s drop constraint %I', r.tbl, r.conname);
  end loop;
end $$;
alter table users add constraint users_default_currency_check
  check (default_currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));
alter table users add constraint users_second_currency_check
  check (second_currency is null or second_currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));
alter table money_transactions add constraint money_transactions_currency_check
  check (currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));
alter table money_recurring add constraint money_recurring_currency_check
  check (currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));
alter table money_categories add constraint money_categories_limit_currency_check
  check (limit_currency is null or limit_currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));
alter table money_goals add constraint money_goals_currency_check
  check (currency in ('EUR', 'KZT', 'USD', 'AZN', 'TRY'));

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

-- 016: владелец может отключить человеку доступ (решение 20 октября 2026). Данные человека остаются.
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.
alter table users add column if not exists blocked boolean not null default false;

-- ---------------------------------------------------------------- 017: язык пользователя (3 ноября 2026)
-- ru | en | kk; пусто — язык Telegram, иначе русский. Ставит приложение (PATCH /me) и бот при первом сообщении.
alter table users add column if not exists language text;
alter table users drop constraint if exists users_language_check;
alter table users add constraint users_language_check check (language is null or language in ('ru', 'en', 'kk'));

-- ---------------------------------------------------------------- 018: азербайджанский язык (3 ноября 2026)
alter table users drop constraint if exists users_language_check;
alter table users add constraint users_language_check check (language is null or language in ('ru', 'en', 'kk', 'az'));
