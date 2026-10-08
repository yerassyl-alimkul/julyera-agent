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
