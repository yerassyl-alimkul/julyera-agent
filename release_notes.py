"""
Заметка о релизе: после деплоя бот один раз присылает владельцу (is_admin), что изменилось.

Перед каждым релизом обнови RELEASE_ID (новое уникальное значение) и RELEASE_TEXT (по-русски, простыми
словами). При запуске сервиса main.py вызывает announce(): строка в release_log вставляется ДО отправки,
поэтому перезапуски и несколько экземпляров не дублируют сообщение. Пока таблицы нет (миграция 003
не выполнена), announce() ничего не делает.
"""
import logging

logger = logging.getLogger(__name__)

RELEASE_ID = "2026-10-09-wallet"
RELEASE_TEXT = """🚀 Обновление JulYera

• Внизу теперь три вкладки: «Задачи», «Заметки» и «Кошелёк».
• «Задачи»: сверху переключатель «Список / Календарь».
• «Кошелёк»: «Расходы», «Доходы» и «Динамика» — столбики за 6 месяцев и круговая диаграмма расходов по категориям. Кнопка «Категории» прямо в кошельке.
• Повторяющиеся траты: в форме траты переключатель «Повторять» — каждую неделю, каждые 2 недели, каждый месяц или каждый год. Следующие операции появляются сами.
• Исправлено: форма нового повторяющегося платежа больше не прячется под списком.
• Теперь после каждого обновления я присылаю такое сообщение."""


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
