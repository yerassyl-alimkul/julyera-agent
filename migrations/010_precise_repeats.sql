-- 010: точные повторы задач (task_service.next_occurrence): по будням, по дням недели, каждый месяц того же числа,
-- каждый год. Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен.

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
