-- 016: владелец может отключить человеку доступ (решение 20 октября 2026). Данные человека остаются.
-- Выполнить целиком в Supabase SQL Editor. Повторный запуск безопасен. Только добавляет.
alter table users add column if not exists blocked boolean not null default false;
