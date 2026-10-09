"""
Заметка о релизе: после деплоя бот один раз присылает всем пользователям, что изменилось (решение владельца
8 октября 2026). Текст пиши так, чтобы он был понятен любому пользователю, без технических подробностей.

Перед каждым релизом обнови RELEASE_ID (новое уникальное значение) и RELEASE_TEXT (по-русски, простыми
словами). При запуске сервиса main.py вызывает announce(): строка в release_log вставляется ДО отправки,
поэтому перезапуски и несколько экземпляров не дублируют сообщение. Пока таблицы нет (миграция 003
не выполнена), announce() ничего не делает.
"""
import logging

logger = logging.getLogger(__name__)

RELEASE_ID = "2026-10-15-savings-goals"
RELEASE_TEXT = """🎯 Обновление JulYera: цели накопления

• В кошельке появилась вкладка «Цели». Нажмите «+», чтобы создать цель — например, «Отпуск — 1500 €» — и, если хотите, дату, к которой нужно накопить. Приложение подскажет, сколько откладывать в месяц, чтобы успеть.
• «Отложить» под целью — внести сумму, там же можно снять и посмотреть историю. Отложенное не считается расходом.
• В боте: /save — прогресс по целям, /save 100 отпуск — отложить, /save -50 отпуск — снять.
• Когда цель будет достигнута, бот поздравит 🎉"""


async def announce(bot, supabase, users: list[dict], reply_markup=None) -> bool:
    """Отправить заметку пользователям, если этот релиз ещё не объявлялся. True — отправлено."""
    try:
        supabase.table("release_log").insert({"id": RELEASE_ID}).execute()
    except Exception as exc:
        if getattr(exc, "code", None) != "23505" and "duplicate key" not in str(exc):
            logger.warning("Заметка о релизе не отправлена: %s", type(exc).__name__)
        return False  # уже объявлен (или таблицы ещё нет)
    sent = False
    for user in users:
        chat_id = user.get("telegram_user_id")
        if not chat_id:
            continue
        try:
            await bot.send_message(chat_id=chat_id, text=RELEASE_TEXT, reply_markup=reply_markup)
            sent = True
        except Exception as exc:
            logger.warning("Заметка о релизе не доставлена: %s", type(exc).__name__)
    return sent
