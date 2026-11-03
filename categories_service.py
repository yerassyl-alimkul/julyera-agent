"""
Категории задач у каждого пользователя свои: название, цвет из палитры, порядок.

В tasks.category хранится название текстом. Допустимость названия проверяет сервер
по списку пользователя (ограничения CHECK в базе больше нет). Переименование меняет
название и в задачах — атомарно, через SQL-функцию rename_category (см. schema.sql).
Каждый запрос фильтруется по user_id: backend ходит в базу с ключом service_role.
"""
from i18n import tr
import i18n
from typing import Optional

from database import supabase

TABLE = "categories"

# Палитра из дизайна: четыре прежних цвета и четыре новых в той же пастельной гамме,
# на всех читается тёмный текст (--ink #17181C, --ink-meta #33353D).
PALETTE = [
    "#85AACB",  # голубой (Study)
    "#E2D47E",  # жёлтый (Work)
    "#A9C89D",  # зелёный (Personal)
    "#DCE5FB",  # светло-голубой (UniMe)
    "#F2B8A2",  # персиковый
    "#D9C2EC",  # лавандовый
    "#F4C8D8",  # розовый
    "#B9E0DA",  # мятный
]
DEFAULT_NAME = "Общее"
DEFAULT_NAMES = {"ru": "Общее", "en": "General", "kk": "Жалпы"}  # на языке Telegram нового пользователя
DEFAULT_COLOR = "#A9C89D"
NAME_MAX = 30


class CategoryError(Exception):
    """Ошибка операции с категорией; status — HTTP-код для ответа (404, 409, 422)."""

    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _clean_name(name: Optional[str]) -> str:
    cleaned = (name or "").strip()
    if not 1 <= len(cleaned) <= NAME_MAX:
        raise CategoryError(422, tr('Название категории — от 1 до {0} символов', NAME_MAX), "bad_name")
    return cleaned


def _clean_color(color: Optional[str]) -> str:
    value = (color or "").strip().upper()
    if value not in PALETTE:
        raise CategoryError(422, tr("Цвет нужно выбрать из палитры"), "bad_color")
    return value


def list_categories(user_id: str) -> list[dict]:
    rows = supabase.table(TABLE).select("*").eq("user_id", user_id).execute().data
    return sorted(rows, key=lambda c: (c.get("sort_order", 0), c.get("created_at") or ""))


def get_category(category_id: str, user_id: str) -> Optional[dict]:
    result = supabase.table(TABLE).select("*").eq("id", category_id).eq("user_id", user_id).execute()
    return result.data[0] if result.data else None


def _find_by_name(user_id: str, name: str, exclude_id: Optional[str] = None) -> Optional[dict]:
    wanted = name.strip().lower()
    for cat in list_categories(user_id):
        if cat["name"].lower() == wanted and cat["id"] != exclude_id:
            return cat
    return None


def resolve_name(user_id: str, name: Optional[str]) -> Optional[str]:
    """Название категории пользователя в том виде, как оно сохранено (без учёта регистра), или None."""
    if not name:
        return None
    found = _find_by_name(user_id, name)
    return found["name"] if found else None


def fallback_name(user_id: str) -> str:
    """Запасная категория: первая по порядку. Если категорий почему-то нет — создаём «Общее»."""
    cats = list_categories(user_id)
    return cats[0]["name"] if cats else ensure_default(user_id)["name"]


def names(user_id: str) -> list[str]:
    return [c["name"] for c in list_categories(user_id)]


def create_category(user_id: str, name: str, color: str) -> dict:
    name, color = _clean_name(name), _clean_color(color)
    if _find_by_name(user_id, name):
        raise CategoryError(409, tr("Категория с таким названием уже есть"), "duplicate")
    existing = list_categories(user_id)
    order = max((c.get("sort_order", 0) for c in existing), default=-1) + 1
    result = supabase.table(TABLE).insert(
        {"user_id": user_id, "name": name, "color": color, "sort_order": order}
    ).execute()
    if not result.data:
        raise RuntimeError("Не удалось создать категорию")
    return result.data[0]


def ensure_default(user_id: str, lang: str = "ru") -> dict:
    """У пользователя всегда есть хотя бы одна категория: новому создаём «Общее» (на его языке)."""
    cats = list_categories(user_id)
    if cats:
        return cats[0]
    return create_category(user_id, DEFAULT_NAMES.get(lang, DEFAULT_NAME), DEFAULT_COLOR)


def update_category(category_id: str, user_id: str, name: Optional[str] = None, color: Optional[str] = None) -> dict:
    current = get_category(category_id, user_id)
    if not current:
        raise CategoryError(404, tr("Категория не найдена"), "not_found")
    if color is not None:
        color = _clean_color(color)
    if name is not None:
        name = _clean_name(name)
        if _find_by_name(user_id, name, exclude_id=category_id):
            raise CategoryError(409, tr("Категория с таким названием уже есть"), "duplicate")
        if name != current["name"]:
            # Атомарно меняет название и в категории, и во всех задачах пользователя с этой категорией.
            supabase.rpc("rename_category", {"p_user": user_id, "p_id": category_id, "p_name": name}).execute()
    if color is not None and color != current["color"]:
        supabase.table(TABLE).update({"color": color}).eq("id", category_id).eq("user_id", user_id).execute()
    return get_category(category_id, user_id)


def reorder(user_id: str, ids: list[str]) -> list[dict]:
    """Порядок задаётся полным списком id категорий пользователя."""
    cats = list_categories(user_id)
    if sorted(ids) != sorted(c["id"] for c in cats) or len(set(ids)) != len(ids):
        raise CategoryError(422, tr("Нужен полный список категорий без повторов"), "bad_order")
    for order, category_id in enumerate(ids):
        supabase.table(TABLE).update({"sort_order": order}).eq("id", category_id).eq("user_id", user_id).execute()
    return list_categories(user_id)


def task_count(user_id: str, name: str) -> int:
    rows = supabase.table("tasks").select("id").eq("user_id", user_id).eq("category", name).execute().data
    return len(rows)


def delete_category(category_id: str, user_id: str, move_to: Optional[str] = None) -> None:
    current = get_category(category_id, user_id)
    if not current:
        raise CategoryError(404, tr("Категория не найдена"), "not_found")
    if len(list_categories(user_id)) <= 1:
        raise CategoryError(409, tr("Последнюю категорию удалить нельзя"), "last_category")
    count = task_count(user_id, current["name"])
    if count:
        if not move_to:
            raise CategoryError(409, tr('В категории задач: {0}. Укажите, куда их перенести', count), "has_tasks")
        target = get_category(move_to, user_id)
        if not target or target["id"] == category_id:
            raise CategoryError(404, tr("Категория для переноса не найдена"), "move_to_not_found")
        supabase.table("tasks").update({"category": target["name"]}).eq("user_id", user_id).eq(
            "category", current["name"]).execute()
    supabase.table(TABLE).delete().eq("id", category_id).eq("user_id", user_id).execute()
