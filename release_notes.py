"""
Заметка о релизе: после деплоя бот один раз присылает владельцу (is_admin), что изменилось.

Перед каждым релизом обнови RELEASE_ID (новое уникальное значение) и RELEASE_TEXT (по-русски, простыми
словами). При запуске сервиса main.py вызывает announce(): строка в release_log вставляется ДО отправки,
поэтому перезапуски и несколько экземпляров не дублируют сообщение. Пока таблицы нет (миграция 003
не выполнена), announce() ничего не делает.
"""
import logging

logger = logging.getLogger(__name__)

RELEASE_ID = "2026-10-09-applepay"
RELEASE_TEXT = """🚀 Обновление JulYera

• Траты из Apple Pay теперь записываются сами: после каждой оплаты телефоном или часами я пришлю «💳 Apple Pay: магазин · сумма · категория».
• Категорию подбираю по названию магазина. Если ошибся — нажмите «Категория» и выберите правильную: для этого магазина я запомню её навсегда.
• Одна и та же покупка дважды не записывается, возвраты пока не записываются.
• Как подключить: напишите мне /applepay — пришлю пошаговую инструкцию для «Команд» на iPhone и ваш ключ."""


async def announce(bot, supabase, admins: list[dict]) -> bool:
    """Отправить заметку владельцам, если этот релиз ещё не объявлялся. True — отправлено."""
    try:
        supabase.table("release_log").insert({"id": RELEASE_ID}).execute()
    except Exception as exc:
        if getattr(exc, "code", None) != "23505" and "duplicate key" not in str(exc):
            logger.warning("Заметка о релизе не отправлена: %s", type(exc).__name__)
        return False  # уже объявлен (или таблицы ещё нет)
    sent = False
    for admin in admins:
        chat_id = admin.get("telegram_user_id")
        if not chat_id:
            continue
        try:
            await bot.send_message(chat_id=chat_id, text=RELEASE_TEXT)
            sent = True
        except Exception as exc:
            logger.warning("Заметка о релизе не доставлена: %s", type(exc).__name__)
    return sent
