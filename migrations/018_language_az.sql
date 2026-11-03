-- 018: азербайджанский язык (решение владельца 3 ноября 2026) — к ru, en, kk добавляется az.
alter table users drop constraint if exists users_language_check;
alter table users add constraint users_language_check check (language is null or language in ('ru', 'en', 'kk', 'az'));
