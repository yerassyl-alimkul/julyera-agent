-- 011: цели накопления (goals_service.py). Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен.

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
