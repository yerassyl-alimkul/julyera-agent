-- 017: язык приложения и бота у пользователя (решение владельца 3 ноября 2026).
-- ru | en | kk; пусто — язык Telegram, иначе русский. Ставит приложение (PATCH /me) и бот при первом сообщении.
alter table users add column if not exists language text;
alter table users drop constraint if exists users_language_check;
alter table users add constraint users_language_check check (language is null or language in ('ru', 'en', 'kk'));
