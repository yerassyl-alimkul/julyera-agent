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

RELEASE_ID = "2026-10-14-repeats-steps-notes"
RELEASE_TEXT = """✨ Обновление JulYera

• Повторы задач стали точными: в «Изменить» и при создании задачи вручную появился блок «Повторять» — каждый день, по будням, по выбранным дням недели (например, пн и ср), каждый месяц того же числа или каждый год. В списке у таких задач значок «↻ пн, ср».
• «Разбей на шаги»: кнопка в задаче — ИИ сам добавит 3–7 шагов, лишние можно убрать крестиком.
• «Задачи из заметки»: кнопка под заметкой находит в ней дела и показывает их с галочками — добавляются только отмеченные. В боте то же самое: /split и текст.
• В календаре появилась кнопка «Сегодня» — быстро вернуться к текущему дню."""


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
