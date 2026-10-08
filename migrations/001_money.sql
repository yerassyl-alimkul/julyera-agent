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
