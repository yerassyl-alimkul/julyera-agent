-- 012: время «с — до» у задач и пять валют с парой «основная + дополнительная».
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен.

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
