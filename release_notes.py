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

RELEASE_ID = "2026-10-16-settings-statement-currencies"
RELEASE_TEXT = """✨ Большое обновление JulYera

• Настройки в одном месте: меню → «Настройки». Там категории задач и отдельно — категории кошелька, валюты, выписка, Google Calendar с понятным объяснением, тема, экспорт и резервная копия.
• Время «с — до»: у задачи можно указать начало и конец, например 09:00–10:30. В Google Calendar событие будет ровно на это время.
• Новые валюты: доллар, манат и лира. В «Кошелёк → Валюты» выберите основную и дополнительную — например, евро и тенге или манат и евро.
• Выписка PDF в стиле JulYera: кошелёк → «Выписка». Итоги, категории, цели и все операции за период. В Telegram файл придёт в бот.
• «Динамика» переехала в «Аналитику» — графики теперь под итогами периода.
• Заметки можно удалять свайпом влево.
• Диктовка в приложении теперь работает как голосовые в боте: нажмите микрофон, скажите, нажмите ещё раз."""


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
