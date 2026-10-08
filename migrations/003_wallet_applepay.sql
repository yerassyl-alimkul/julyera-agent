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
