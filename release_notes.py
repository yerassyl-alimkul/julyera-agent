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

RELEASE_ID = "2026-10-13-reminders-weekly-backup"
RELEASE_TEXT = """🔔 Обновление JulYera

• Напоминание перед задачей: за 30 минут до задачи со временем бот пишет «🔔 В 15:00 (через 25 мин): …» с кнопкой «Готово». Время можно поменять в меню приложения (15 мин, 30 мин, 1 час или выключить) или командой /remind.
• Итоги недели: в воскресенье вечером вместо обычной сводки — что сделано за неделю, что не закрыто, план на следующую неделю по дням и сколько потрачено.
• «Завтра спишется»: вечером накануне повторяющегося платежа (подписка, аренда) бот предупредит о сумме.
• Резервная копия: каждое воскресенье в 21:00 бот присылает файл с вашими задачами, заметками и тратами — только вашими. Сохраните его на всякий случай. Получить копию сразу — команда /backup."""


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
