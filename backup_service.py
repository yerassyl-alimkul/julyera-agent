"""
Резервная копия данных одного пользователя файлом JSON (решение владельца 10 октября 2026): каждое воскресенье
в 21:00 бот присылает её в чат пользователя (digest_service.backup_due), по запросу — командой /backup.

В файл попадает только сам пользователь: задачи с подзадачами и текстом комментариев, заметки, категории,
деньги (категории, операции, повторяющиеся платежи). Файлы из комментариев (фото, PDF) — только имена:
сами файлы остаются в хранилище. Токены календаря, ключ доступа и служебные журналы не выгружаются.
В логи пишется только количество строк.
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import money_service
import users_service
from database import supabase

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1


def _all(table: str, user_id: str, columns: str = "*") -> list[dict]:
    return money_service._fetch_all(lambda: supabase.table(table).select(columns).eq("user_id", user_id))


def build(user_id: str) -> tuple[str, bytes, int]:
    """(имя файла, содержимое, сколько всего записей)."""
    user = users_service.get_by_id(user_id) or {}
    tasks = _all("tasks", user_id)
    task_ids = {t["id"] for t in tasks}
    subtasks = []
    ids = sorted(task_ids)
    for i in range(0, len(ids), 200):  # у подзадач нет user_id — берём по своим задачам
        subtasks += supabase.table("subtasks").select("*").in_("task_id", ids[i:i + 200]).execute().data
    try:
        comments = [{k: c.get(k) for k in ("id", "task_id", "text", "file_name", "file_type", "created_at")}
                    for c in _all("task_comments", user_id)]
    except Exception as exc:
        logger.warning("Комментарии в копию не попали: %s", type(exc).__name__)
        comments = []
    data = {
        "format": "julyera-backup", "version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user": {"name": user.get("name"), "timezone": user.get("timezone"),
                 "default_currency": user.get("default_currency")},
        "tasks": tasks,
        "subtasks": [s for s in subtasks if s.get("task_id") in task_ids],
        "task_comments": comments,
        "notes": _all("notes", user_id),
        "categories": _all("categories", user_id),
        "money_categories": _all(money_service.CAT_TABLE, user_id),
        "money_transactions": _all(money_service.TX_TABLE, user_id),
        "money_recurring": _all(money_service.REC_TABLE, user_id),
    }
    count = sum(len(v) for v in data.values() if isinstance(v, list))
    body = json.dumps(data, ensure_ascii=False, indent=1, default=lambda v: str(v) if isinstance(v, Decimal) else str(v))
    day = datetime.now(timezone.utc).date().isoformat()
    logger.info("Резервная копия собрана: %s записей", count)
    return f"julyera-backup-{day}.json", body.encode("utf-8"), count


def caption(count: int) -> str:
    return (f"🗄 Резервная копия ваших данных: задачи, заметки, траты ({count} записей). "
            "Сохраните файл — если что-то пропадёт, по нему можно всё восстановить.")
