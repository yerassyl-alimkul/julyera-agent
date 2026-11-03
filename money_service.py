"""
Учёт расходов и доходов (задача №3): категории денег, операции, итоги, повторяющиеся платежи.

Изоляция как у задач: backend ходит в базу ключом service_role, поэтому каждая функция принимает
user_id обязательным аргументом и каждый запрос фильтруется по нему. Ссылки на другие строки
(category_id, move_to) тоже проверяются на принадлежность тому же пользователю.

Деньги: в базе numeric(14,2), здесь Decimal, наружу строкой с двумя знаками ("1240.50").
Валюты не смешиваются и не пересчитываются: все итоги отдельно по каждой.
Дата операции — date без времени ("YYYY-MM-DD"); «сегодня» считается в поясе users.timezone.
В логи не пишем суммы, заметки и тексты сообщений — только тип события, id и количество.
"""
from i18n import tr
import i18n
import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

import rates_service
import users_service
from categories_service import PALETTE
from database import supabase

logger = logging.getLogger(__name__)

# Чтобы добавить валюту: сюда, в CHECK миграции, в CURRENCY_MARKERS, _CURRENCY_TOKENS и CURRENCY_META во фронтенде.
# Курс берётся у Нацбанка РК (там есть все пять). У пользователя пара: основная + дополнительная (миграция 012).
CURRENCIES = ("EUR", "KZT", "USD", "AZN", "TRY")
KINDS = ("expense", "income")
SOURCES = ("app", "text", "voice", "recurring", "applepay")

CAT_TABLE = "money_categories"
TX_TABLE = "money_transactions"
REC_TABLE = "money_recurring"

AMOUNT_MAX = Decimal("1000000000000")  # 10^12, как CHECK в базе
CENT = Decimal("0.01")
NOTE_MAX = 200
NAME_MAX = 30
LIST_LIMIT_MAX = 200
PAGE = 1000          # Supabase отдаёт не больше 1000 строк за запрос — итоги читаем порциями
DATE_RANGE_YEARS = 5
CATCH_UP_MAX = 12    # повторяющийся платёж догоняет не больше 12 пропущенных месяцев за прогон
FALLBACK_NAME = "Прочее"
FALLBACK_NAMES = ("прочее", "other", "басқа", "digər")  # «Прочее» на языках интерфейса

# Стартовый набор — как в migrations/001_money.sql (цвета из палитры категорий задач).
DEFAULT_SET = {
    "expense": [("Еда", "#A9C89D"), ("Транспорт", "#85AACB"), ("Жильё", "#E2D47E"), ("Здоровье", "#DCE5FB"),
                ("Развлечения", "#F2B8A2"), ("Подписки", "#D9C2EC"), ("Одежда", "#F4C8D8"), ("Прочее", "#B9E0DA")],
    "income": [("Зарплата", "#A9C89D"), ("Прочее", "#B9E0DA")],
}
# Названия стартового набора на других языках (новый пользователь получает их на языке своего Telegram).
DEFAULT_NAMES = {
    "en": {"Еда": "Food", "Транспорт": "Transport", "Жильё": "Housing", "Здоровье": "Health",
           "Развлечения": "Entertainment", "Подписки": "Subscriptions", "Одежда": "Clothes", "Прочее": "Other",
           "Зарплата": "Salary"},
    "kk": {"Еда": "Тамақ", "Транспорт": "Көлік", "Жильё": "Тұрғын үй", "Здоровье": "Денсаулық",
           "Развлечения": "Ойын-сауық", "Подписки": "Жазылымдар", "Одежда": "Киім", "Прочее": "Басқа",
           "Зарплата": "Жалақы"},
    "az": {"Еда": "Yemək", "Транспорт": "Nəqliyyat", "Жильё": "Mənzil", "Здоровье": "Sağlamlıq",
           "Развлечения": "Əyləncə", "Подписки": "Abunələr", "Одежда": "Geyim", "Прочее": "Digər",
           "Зарплата": "Maaş"},
}


class MoneyError(Exception):
    """Ошибка операции с деньгами; status — HTTP-код ответа (404, 409, 422), message — понятный текст."""

    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# ---------------------------------------------------------------- значения и проверки

def fmt(value) -> str:
    """Decimal (или число из базы) → строка с двумя знаками: "1240.50"."""
    return str(Decimal(str(value)).quantize(CENT))


def parse_amount(value) -> Decimal:
    """Сумма из запроса: строка или число, запятая как десятичный знак, пробелы между разрядами допустимы.
    Больше 0, меньше 10^12, не больше двух знаков после запятой."""
    if isinstance(value, bool) or value is None:
        raise MoneyError(422, tr("Укажите сумму числом"), "bad_amount")
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise MoneyError(422, tr("Сумма должна быть положительным числом, например 3,50"), "bad_amount")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise MoneyError(422, tr("Сумма должна быть положительным числом, например 3,50"), "bad_amount")
    if amount != amount.quantize(CENT):
        raise MoneyError(422, tr("В сумме не больше двух знаков после запятой"), "bad_amount")
    if amount <= 0:
        raise MoneyError(422, tr("Сумма должна быть больше нуля"), "bad_amount")
    if amount >= AMOUNT_MAX:
        raise MoneyError(422, tr("Слишком большая сумма"), "bad_amount")
    return amount.quantize(CENT)


def clean_currency(value) -> str:
    cur = str(value or "").strip().upper()
    if cur not in CURRENCIES:
        raise MoneyError(422, tr("Валюта: {0}", tr(" или ").join(CURRENCIES)), "bad_currency")
    return cur


def clean_kind(value) -> str:
    if value not in KINDS:
        raise MoneyError(422, tr("Тип операции: expense (расход) или income (доход)"), "bad_kind")
    return value


def clean_note(value) -> Optional[str]:
    note = (value or "").strip()
    if len(note) > NOTE_MAX:
        raise MoneyError(422, tr('Заметка — не длиннее {0} символов', NOTE_MAX), "bad_note")
    return note or None


def clean_name(value) -> str:
    name = (value or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise MoneyError(422, tr('Название категории — от 1 до {0} символов', NAME_MAX), "bad_name")
    return name


def clean_color(value) -> str:
    color = (value or "").strip().upper()
    if color not in PALETTE:
        raise MoneyError(422, tr("Цвет нужно выбрать из палитры"), "bad_color")
    return color


def today_for(user_id: str) -> date:
    """«Сегодня» в поясе пользователя (users.timezone, по умолчанию Europe/Rome)."""
    return datetime.now(ZoneInfo(users_service.timezone_of(user_id))).date()


def parse_day(value, user_id: str) -> date:
    """Дата операции "YYYY-MM-DD" в пределах ± 5 лет от сегодняшней даты пользователя."""
    if isinstance(value, date):
        day = value
    else:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise MoneyError(422, tr("Дата в виде ГГГГ-ММ-ДД"), "bad_date")
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise MoneyError(422, tr("Такой даты нет"), "bad_date")
    today = today_for(user_id)
    if abs(day.year - today.year) > DATE_RANGE_YEARS or abs((day - today).days) > DATE_RANGE_YEARS * 366:
        raise MoneyError(422, tr('Дата должна быть в пределах {0} лет от сегодня', DATE_RANGE_YEARS), "bad_date")
    return day


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(first_of_month: date, n: int) -> date:
    y, m = divmod(first_of_month.month - 1 + n, 12)
    return date(first_of_month.year + y, m + 1, 1)


def _parse_month(value: Optional[str], user_id: str) -> date:
    if not value:
        return _month_start(today_for(user_id))
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise MoneyError(422, tr("Месяц в виде ГГГГ-ММ"), "bad_month")
    try:
        return date(int(value[:4]), int(value[5:]), 1)
    except ValueError:
        raise MoneyError(422, tr("Такого месяца нет"), "bad_month")


def _clamped(year: int, month: int, day_of_month: int) -> date:
    """День месяца с поправкой на короткие месяцы: 31 в феврале → 28 или 29."""
    return date(year, month, min(day_of_month, calendar.monthrange(year, month)[1]))


def _is_duplicate(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "duplicate key" in str(exc)


def _fetch_all(build) -> list[dict]:
    """Все строки запроса порциями по PAGE (build() строит запрос заново для каждой порции)."""
    rows, start = [], 0
    while True:
        chunk = build().order("id").range(start, start + PAGE - 1).execute().data
        rows.extend(chunk)
        if len(chunk) < PAGE:
            return rows
        start += PAGE


# ---------------------------------------------------------------- пользователь

def default_currency(user_id: str) -> str:
    user = users_service.get_by_id(user_id) or {}
    cur = user.get("default_currency")
    return cur if cur in CURRENCIES else CURRENCIES[0]


def set_default_currency(user_id: str, currency) -> str:
    cur = clean_currency(currency)
    data = {"default_currency": cur}
    if second_currency(user_id) == cur:  # основная и дополнительная не совпадают: меняем их местами
        data["second_currency"] = default_currency(user_id)
    supabase.table("users").update(data).eq("id", user_id).execute()
    return cur


def second_currency(user_id: str) -> str:
    """Дополнительная валюта (вторая кнопка в форме траты). Не задана — вторая из EUR/KZT."""
    user = users_service.get_by_id(user_id) or {}
    main = user.get("default_currency") if user.get("default_currency") in CURRENCIES else CURRENCIES[0]
    cur = user.get("second_currency")
    if cur in CURRENCIES and cur != main:
        return cur
    return "KZT" if main != "KZT" else "EUR"


def set_second_currency(user_id: str, currency) -> str:
    cur = clean_currency(currency)
    if cur == default_currency(user_id):
        raise MoneyError(422, tr("Дополнительная валюта должна отличаться от основной"), "same_currency")
    supabase.table("users").update({"second_currency": cur}).eq("id", user_id).execute()
    return cur


# ---------------------------------------------------------------- категории

def list_categories(user_id: str, kind: Optional[str] = None) -> list[dict]:
    query = supabase.table(CAT_TABLE).select("*").eq("user_id", user_id)
    if kind:
        query = query.eq("kind", clean_kind(kind))
    rows = query.execute().data
    for row in rows:  # лимит наружу — строкой с двумя знаками, как все суммы
        if row.get("monthly_limit") is not None:
            row["monthly_limit"] = fmt(row["monthly_limit"])
    return sorted(rows, key=lambda c: (c["kind"], c.get("sort_order", 0), c.get("created_at") or ""))


def get_category(category_id: str, user_id: str) -> Optional[dict]:
    if not _is_uuid(category_id):
        return None
    rows = supabase.table(CAT_TABLE).select("*").eq("id", category_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def _is_uuid(value) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                             str(value or "")))


def _find_by_name(user_id: str, kind: str, name: str, exclude_id: Optional[str] = None) -> Optional[dict]:
    wanted = name.strip().lower()
    return next((c for c in list_categories(user_id, kind)
                 if c["name"].lower() == wanted and c["id"] != exclude_id), None)


def create_category(user_id: str, kind, name, color) -> dict:
    kind, name, color = clean_kind(kind), clean_name(name), clean_color(color)
    if _find_by_name(user_id, kind, name):
        raise MoneyError(409, tr("Категория с таким названием уже есть"), "duplicate")
    order = max((c.get("sort_order", 0) for c in list_categories(user_id, kind)), default=-1) + 1
    rows = supabase.table(CAT_TABLE).insert(
        {"user_id": user_id, "kind": kind, "name": name, "color": color, "sort_order": order}).execute().data
    if not rows:
        raise RuntimeError("Не удалось создать категорию денег")
    return rows[0]


def ensure_defaults(user_id: str, lang: str = "ru") -> None:
    """Стартовый набор категорий (для нового пользователя при одобрении) на языке lang (ru | en | kk).
    Существующие не трогает."""
    names = DEFAULT_NAMES.get(lang, {})
    for kind, items in DEFAULT_SET.items():
        if list_categories(user_id, kind):
            continue
        supabase.table(CAT_TABLE).insert([
            {"user_id": user_id, "kind": kind, "name": names.get(name, name), "color": color, "sort_order": i}
            for i, (name, color) in enumerate(items)
        ]).execute()


_UNSET = object()


def update_category(category_id: str, user_id: str, name=None, color=None, monthly_limit=_UNSET) -> dict:
    """Название, цвет и месячный лимит (только у расходов; None или "" — снять). Тип (kind) не меняется.
    Лимит запоминается в основной валюте пользователя на момент задания (limit_currency)."""
    current = get_category(category_id, user_id)
    if not current:
        raise MoneyError(404, tr("Категория не найдена"), "not_found")
    changes = {}
    if name is not None:
        name = clean_name(name)
        if _find_by_name(user_id, current["kind"], name, exclude_id=category_id):
            raise MoneyError(409, tr("Категория с таким названием уже есть"), "duplicate")
        changes["name"] = name
    if color is not None:
        changes["color"] = clean_color(color)
    if monthly_limit is not _UNSET:
        if monthly_limit in (None, ""):
            changes.update(monthly_limit=None, limit_currency=None)
        else:
            if current["kind"] != "expense":
                raise MoneyError(422, tr("Лимит бывает только у категорий расходов"), "bad_limit")
            changes.update(monthly_limit=str(parse_amount(monthly_limit)), limit_currency=default_currency(user_id))
    if changes:
        supabase.table(CAT_TABLE).update(changes).eq("id", category_id).eq("user_id", user_id).execute()
    cat = get_category(category_id, user_id)
    if cat and cat.get("monthly_limit") is not None:
        cat["monthly_limit"] = fmt(cat["monthly_limit"])
    return cat


def reorder_categories(user_id: str, kind, ids: list[str]) -> list[dict]:
    kind = clean_kind(kind)
    cats = list_categories(user_id, kind)
    if sorted(ids) != sorted(c["id"] for c in cats) or len(set(ids)) != len(ids):
        raise MoneyError(422, tr("Нужен полный список категорий этого типа без повторов"), "bad_order")
    for order, category_id in enumerate(ids):
        supabase.table(CAT_TABLE).update({"sort_order": order}).eq("id", category_id).eq("user_id", user_id).execute()
    return list_categories(user_id, kind)


def _usage_count(user_id: str, category_id: str) -> int:
    tx = supabase.table(TX_TABLE).select("id").eq("user_id", user_id).eq("category_id", category_id).limit(1).execute().data
    rec = supabase.table(REC_TABLE).select("id").eq("user_id", user_id).eq("category_id", category_id).limit(1).execute().data
    return len(tx) + len(rec)


def delete_category(category_id: str, user_id: str, move_to: Optional[str] = None) -> None:
    current = get_category(category_id, user_id)
    if not current:
        raise MoneyError(404, tr("Категория не найдена"), "not_found")
    if len(list_categories(user_id, current["kind"])) <= 1:
        raise MoneyError(409, tr("Последнюю категорию этого типа удалить нельзя"), "last_category")
    if _usage_count(user_id, category_id):
        if not move_to:
            raise MoneyError(409, tr("В категории есть операции. Укажите, куда их перенести"), "has_items")
        target = get_category(move_to, user_id)
        if not target or target["id"] == category_id:
            raise MoneyError(404, tr("Категория для переноса не найдена"), "move_to_not_found")
        if target["kind"] != current["kind"]:
            raise MoneyError(422, tr("Переносить можно только в категорию того же типа"), "move_to_kind")
        for table in (TX_TABLE, REC_TABLE):
            supabase.table(table).update({"category_id": target["id"]}).eq("user_id", user_id).eq(
                "category_id", category_id).execute()
    supabase.table(CAT_TABLE).delete().eq("id", category_id).eq("user_id", user_id).execute()


def _own_category(user_id: str, category_id, kind: str) -> dict:
    """Категория для операции: своя и того же типа. Чужая или неподходящая — 422, а не тихая привязка."""
    cat = get_category(str(category_id or ""), user_id)
    if not cat:
        raise MoneyError(422, tr("Категория не найдена среди ваших"), "bad_category")
    if cat["kind"] != kind:
        raise MoneyError(422, tr("Категория другого типа (расход/доход)"), "bad_category")
    return cat


def resolve_category(user_id: str, kind: str, name: Optional[str]) -> dict:
    """Категория по названию от модели без учёта регистра; иначе «Прочее», иначе первая по порядку."""
    cats = list_categories(user_id, kind)
    if not cats:
        ensure_defaults(user_id)
        cats = list_categories(user_id, kind)
    by_name = {c["name"].lower(): c for c in cats}
    found = by_name.get((name or "").strip().lower())
    return found or next((by_name[n] for n in FALLBACK_NAMES if n in by_name), cats[0])


# ---------------------------------------------------------------- операции

def _tx_out(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "amount": fmt(row["amount"]), "currency": row["currency"],
        "category_id": row["category_id"], "note": row.get("note"), "spent_on": str(row["spent_on"])[:10],
        "source": row.get("source"), "recurring_id": row.get("recurring_id"), "created_at": row.get("created_at"),
    }


def _get_tx_row(tx_id: str, user_id: str) -> Optional[dict]:
    if not _is_uuid(tx_id):
        return None
    rows = supabase.table(TX_TABLE).select("*").eq("id", tx_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def get_transaction(tx_id: str, user_id: str) -> Optional[dict]:
    row = _get_tx_row(tx_id, user_id)
    return _tx_out(row) if row else None


def create_transaction(user_id: str, data: dict, source: str = "app") -> dict:
    """Операция. data["repeat"] — частота («Повторять» в форме): тогда заодно создаётся правило повтора
    с этой операцией как первой (anchor_date = её дата), и операция привязывается к нему."""
    kind = clean_kind(data.get("kind"))
    repeat = clean_frequency(data["repeat"]) if data.get("repeat") else None
    row = {
        "user_id": user_id,
        "kind": kind,
        "amount": str(parse_amount(data.get("amount"))),
        "currency": clean_currency(data.get("currency")),
        "category_id": _own_category(user_id, data.get("category_id"), kind)["id"],
        "note": clean_note(data.get("note")),
        "spent_on": parse_day(data.get("spent_on") or today_for(user_id), user_id).isoformat(),
        "source": source if source in SOURCES else "app",
    }
    rows = supabase.table(TX_TABLE).insert(row).execute().data
    if not rows:
        raise RuntimeError("Не удалось сохранить операцию")
    tx = rows[0]
    logger.info("Операция создана: %s, источник %s", tx["id"], row["source"])
    if repeat:
        rec = create_recurring(user_id, {
            "kind": kind, "amount": row["amount"], "currency": row["currency"], "category_id": row["category_id"],
            "note": row["note"], "frequency": repeat, "anchor_date": row["spent_on"],
            "as_task": bool(data.get("as_task"))})
        supabase.table(TX_TABLE).update({"recurring_id": rec["id"]}).eq("id", tx["id"]).eq("user_id", user_id).execute()
        tx = {**tx, "recurring_id": rec["id"]}
    if kind == "expense":
        check_limit_alerts(user_id, row["category_id"], row["spent_on"])
    return _tx_out(tx)


def update_transaction(tx_id: str, user_id: str, data: dict) -> Optional[dict]:
    """Меняет только переданные поля. Категорию можно сменить на любую свою того же типа;
    при смене типа нужна категория нового типа."""
    current = _get_tx_row(tx_id, user_id)
    if not current:
        return None
    changes = {}
    kind = current["kind"]
    if data.get("kind") is not None:
        kind = clean_kind(data["kind"])
        changes["kind"] = kind
    if data.get("amount") is not None:
        changes["amount"] = str(parse_amount(data["amount"]))
    if data.get("currency") is not None:
        changes["currency"] = clean_currency(data["currency"])
    if data.get("category_id") is not None or kind != current["kind"]:
        changes["category_id"] = _own_category(user_id, data.get("category_id") or current["category_id"], kind)["id"]
    if "note" in data:
        changes["note"] = clean_note(data["note"])
    if data.get("spent_on") is not None:
        changes["spent_on"] = parse_day(data["spent_on"], user_id).isoformat()
    if changes:
        supabase.table(TX_TABLE).update(changes).eq("id", tx_id).eq("user_id", user_id).execute()
    if (changes.get("category_id") and changes["category_id"] != current["category_id"]
            and current.get("source") == "applepay"):
        remember_merchant(user_id, current.get("note"), changes["category_id"])  # в следующий раз — сразу сюда
    updated = get_transaction(tx_id, user_id)
    if updated and updated["kind"] == "expense" and {"amount", "currency", "category_id", "spent_on", "kind"} & set(changes):
        check_limit_alerts(user_id, updated["category_id"], updated["spent_on"])
    return updated


def delete_transaction(tx_id: str, user_id: str) -> bool:
    if not _get_tx_row(tx_id, user_id):
        return False
    supabase.table(TX_TABLE).delete().eq("id", tx_id).eq("user_id", user_id).execute()
    return True


def list_transactions(user_id: str, date_from=None, date_to=None, kind=None, category_id=None, currency=None,
                      limit: int = LIST_LIMIT_MAX, offset: int = 0) -> list[dict]:
    """Операции за период (по умолчанию текущий месяц), новые сверху; limit не больше 200."""
    if date_from is None and date_to is None:
        start = _month_start(today_for(user_id))
        end = _add_months(start, 1) - timedelta(days=1)
    else:
        start = parse_day(date_from, user_id) if date_from else None
        end = parse_day(date_to, user_id) if date_to else None
    limit = max(1, min(int(limit or LIST_LIMIT_MAX), LIST_LIMIT_MAX))
    offset = max(0, int(offset or 0))
    query = supabase.table(TX_TABLE).select("*").eq("user_id", user_id)
    if start:
        query = query.gte("spent_on", start.isoformat())
    if end:
        query = query.lte("spent_on", end.isoformat())
    if kind:
        query = query.eq("kind", clean_kind(kind))
    if category_id:
        query = query.eq("category_id", str(category_id))
    if currency:
        query = query.eq("currency", clean_currency(currency))
    # id — последний ключ: у операций одной пачки совпадают дата и created_at, а без однозначного порядка
    # страницы через offset пересекаются (одна строка дважды, другая пропадает)
    rows = query.order("spent_on", desc=True).order("created_at", desc=True).order("id").range(
        offset, offset + limit - 1).execute().data
    out = [_tx_out(r) for r in rows]
    main = default_currency(user_id)
    foreign = {t["currency"] for t in out} - {main}
    rates = rates_service.get_rates() if foreign else None
    if foreign and rates_service.can_convert(rates, foreign, main):
        for t in out:
            if t["currency"] != main:  # «≈ 7,70 €» под суммой в списке; только для показа
                t["converted"] = {"amount": fmt(rates_service.convert(Decimal(t["amount"]), t["currency"], main, rates)),
                                  "currency": main}
    return out


def last_transactions(user_id: str, n: int = 5) -> list[dict]:
    rows = supabase.table(TX_TABLE).select("*").eq("user_id", user_id).order(
        "created_at", desc=True).order("id").range(0, n - 1).execute().data
    return [_tx_out(r) for r in rows]


def _period_rows(user_id: str, start: date, end_exclusive: date) -> list[dict]:
    return _fetch_all(lambda: supabase.table(TX_TABLE).select("kind,amount,currency,category_id,spent_on")
                      .eq("user_id", user_id).gte("spent_on", start.isoformat()).lt("spent_on", end_exclusive.isoformat()))


def summary(user_id: str, month: Optional[str] = None) -> dict:
    """Итоги месяца по каждой валюте: доходы, расходы, баланс, расходы по категориям (сумма и доля),
    суммы по дням (для заголовков дней в списке), доходы и расходы предыдущего месяца и разница расходов.
    Валюта без операций в этом месяце не появляется. Клиент суммы не считает, только показывает."""
    start = _parse_month(month, user_id)
    prev_start, next_start = _add_months(start, -1), _add_months(start, 1)
    rows = _period_rows(user_id, prev_start, next_start)
    cats = {c["id"]: c for c in list_categories(user_id)}

    def totals(selected):
        out = {}
        for r in selected:
            t = out.setdefault(r["currency"], {"income": Decimal(0), "expense": Decimal(0), "by_cat": {},
                                               "by_cat_income": {}, "by_day": {}})
            amount = Decimal(str(r["amount"]))
            t[r["kind"]] += amount
            if r["kind"] == "income":
                t["by_cat_income"][r["category_id"]] = t["by_cat_income"].get(r["category_id"], Decimal(0)) + amount
            day = t["by_day"].setdefault(str(r["spent_on"])[:10], {"income": Decimal(0), "expense": Decimal(0)})
            day[r["kind"]] += amount
            if r["kind"] == "expense":
                t["by_cat"][r["category_id"]] = t["by_cat"].get(r["category_id"], Decimal(0)) + amount
        return out

    cur_rows = [r for r in rows if str(r["spent_on"])[:10] >= start.isoformat()]
    prev_rows = [r for r in rows if str(r["spent_on"])[:10] < start.isoformat()]
    current, previous = totals(cur_rows), totals(prev_rows)
    currencies = []
    for cur in CURRENCIES:
        if cur not in current:
            continue
        t, p = current[cur], previous.get(cur, {"income": Decimal(0), "expense": Decimal(0)})
        by_category = _breakdown(cats, t["by_cat"], t["expense"])
        currencies.append({
            "currency": cur, "income": fmt(t["income"]), "expense": fmt(t["expense"]),
            "balance": fmt(t["income"] - t["expense"]), "by_category": by_category,
            "income_by_category": _breakdown(cats, t["by_cat_income"], t["income"]),
            "days": [{"day": d, "income": fmt(v["income"]), "expense": fmt(v["expense"])}
                     for d, v in sorted(t["by_day"].items(), reverse=True)],
            "previous": {"income": fmt(p["income"]), "expense": fmt(p["expense"]),
                         "expense_change": fmt(t["expense"] - p["expense"]),
                         "income_change": fmt(t["income"] - p["income"])},
        })
    main_cur = default_currency(user_id)
    rates = rates_service.get_rates() if (set(current) | set(previous)) - {main_cur} else None
    return {"month": start.strftime("%Y-%m"), "currencies": currencies,
            "main": _main_block(cats, current, previous, main_cur, rates),
            "rate": rates_service.rate_info(rates, main_cur)}


def _breakdown(cats: dict, parts: dict, total: Decimal) -> list[dict]:
    out = []
    for cat_id, amount in sorted(parts.items(), key=lambda kv: kv[1], reverse=True):
        cat = cats.get(cat_id, {})
        share = int((amount * 100 / total).quantize(Decimal(1))) if total else 0
        out.append({"category_id": cat_id, "name": cat.get("name", "?"), "color": cat.get("color"),
                    "amount": fmt(amount), "share": share})
    return out


def _main_block(cats: dict, current: dict, previous: dict, main: str, rates: Optional[dict]) -> Optional[dict]:
    """Итоги месяца в основной валюте пользователя: каждая валюта пересчитана по сегодняшнему курсу.
    parts — исходные суммы по валютам с пересчётом (для строк «в тенге: 4 000 ₸ ≈ 7,70 €»).
    None — если нужен курс, а его нет (тогда клиент показывает итоги по валютам раздельно)."""
    if not rates_service.can_convert(rates, set(current) | set(previous), main):
        return None

    def conv(amount, cur):
        return rates_service.convert(amount, cur, main, rates)

    approx_cats = set()  # категории и дни, в итог которых вошёл пересчёт (там клиент ставит «≈»)

    def merged(totals: dict, key: str) -> dict:
        out = {}
        for cur, t in totals.items():
            for cat_id, amount in t.get(key, {}).items():
                out[cat_id] = out.get(cat_id, Decimal(0)) + conv(amount, cur)
                if cur != main:
                    approx_cats.add((key, cat_id))
        return out

    def marked(items: list, key: str) -> list:
        return [{**c, "approx": (key, c["category_id"]) in approx_cats} for c in items]

    def total(totals: dict, kind: str) -> Decimal:
        return sum((conv(t[kind], cur) for cur, t in totals.items()), Decimal(0))

    days = {}
    for cur, t in current.items():
        for d, v in t["by_day"].items():
            day = days.setdefault(d, {"income": Decimal(0), "expense": Decimal(0), "approx_income": False, "approx_expense": False})
            day["income"] += conv(v["income"], cur)
            day["expense"] += conv(v["expense"], cur)
            if cur != main:
                day["approx_income"] = day["approx_income"] or bool(v["income"])
                day["approx_expense"] = day["approx_expense"] or bool(v["expense"])
    income, expense = total(current, "income"), total(current, "expense")
    prev_income, prev_expense = total(previous, "income"), total(previous, "expense")
    by_category = marked(_breakdown(cats, merged(current, "by_cat"), expense), "by_cat")
    by_category, limits = _with_limits(cats, current, by_category, rates)
    parts = {kind: [{"currency": cur, "amount": fmt(current[cur][kind]), "converted": fmt(conv(current[cur][kind], cur))}
                    for cur in CURRENCIES if cur in current and current[cur][kind]]
             for kind in ("income", "expense")}
    return {
        "currency": main, "income": fmt(income), "expense": fmt(expense), "balance": fmt(income - expense),
        "by_category": by_category,
        "limits": limits,
        "income_by_category": marked(_breakdown(cats, merged(current, "by_cat_income"), income), "by_cat_income"),
        "days": [{"day": d, "income": fmt(v["income"]), "expense": fmt(v["expense"]),
                  "approx_income": v["approx_income"], "approx_expense": v["approx_expense"]}
                 for d, v in sorted(days.items(), reverse=True)],
        "parts": parts,
        "previous": {"income": fmt(prev_income), "expense": fmt(prev_expense),
                     "expense_change": fmt(expense - prev_expense), "income_change": fmt(income - prev_income)},
    }


LIMIT_WARN, LIMIT_OVER = 80, 100


def _limit_state(spent: Decimal, limit: Decimal) -> tuple[int, str]:
    pct = int((spent * 100 / limit).to_integral_value()) if limit else 0
    return pct, ("over" if pct >= LIMIT_OVER else "warn" if pct >= LIMIT_WARN else "ok")


def _with_limits(cats: dict, current: dict, by_category: list, rates: Optional[dict]) -> tuple[list, dict]:
    """Добавить к категориям расходов их лимит: потрачено в валюте лимита, процент и состояние (ok/warn/over).
    Категории с лимитом, но без трат в этом месяце, тоже попадают в список (с нулём)."""
    counts = {"ok": 0, "warn": 0, "over": 0}
    have = {c["category_id"] for c in by_category}
    for cat_id, cat in cats.items():
        if cat.get("kind") == "expense" and cat.get("monthly_limit") is not None and cat_id not in have:
            by_category.append({"category_id": cat_id, "name": cat["name"], "color": cat.get("color"),
                                "amount": "0.00", "share": 0, "approx": False})
    out = []
    for c in by_category:
        cat = cats.get(c["category_id"], {})
        if cat.get("monthly_limit") is None:
            out.append(c)
            continue
        limit_cur = cat.get("limit_currency") or CURRENCIES[0]
        if not rates_service.can_convert(rates, {cur for cur, t in current.items() if c["category_id"] in t["by_cat"]}, limit_cur):
            out.append(c)
            continue
        spent = sum((rates_service.convert(t["by_cat"][c["category_id"]], cur, limit_cur, rates)
                     for cur, t in current.items() if c["category_id"] in t["by_cat"]), Decimal(0))
        limit = Decimal(str(cat["monthly_limit"]))
        pct, state = _limit_state(spent, limit)
        counts[state] += 1
        out.append({**c, "limit": {"amount": fmt(limit), "currency": limit_cur, "spent": fmt(spent), "pct": pct,
                                   "state": state, "left": fmt(limit - spent)}})
    return out, counts


def check_limit_alerts(user_id: str, category_id: str, spent_on: str) -> list[str]:
    """После записи траты: если категория перешла 80% или 100% лимита в текущем месяце — один раз написать
    в бот. Возвращает отправленные тексты (для проверок). Сбой здесь не мешает самой операции."""
    try:
        cat = get_category(category_id, user_id)
        if not cat or cat["kind"] != "expense" or cat.get("monthly_limit") is None:
            return []
        today = today_for(user_id)
        month = _month_start(today)
        if str(spent_on)[:7] != month.strftime("%Y-%m"):
            return []  # лимиты — про текущий месяц
        rows = _fetch_all(lambda: supabase.table(TX_TABLE).select("amount,currency").eq("user_id", user_id)
                          .eq("kind", "expense").eq("category_id", category_id)
                          .gte("spent_on", month.isoformat()).lt("spent_on", _add_months(month, 1).isoformat()))
        limit_cur = cat.get("limit_currency") or CURRENCIES[0]
        currencies = {r["currency"] for r in rows}
        rates = rates_service.get_rates() if currencies - {limit_cur} else None
        if not rates_service.can_convert(rates, currencies, limit_cur):
            return []
        spent = sum((rates_service.convert(Decimal(str(r["amount"])), r["currency"], limit_cur, rates) for r in rows), Decimal(0))
        limit = Decimal(str(cat["monthly_limit"]))
        pct, state = _limit_state(spent, limit)
        if state == "ok":
            return []
        key = month.strftime("%Y-%m")
        sent = []

        def claim(threshold: int) -> bool:
            try:
                supabase.table("money_limit_alerts").insert({"user_id": user_id, "category_id": category_id,
                                                              "month": key, "threshold": threshold}).execute()
                return True
            except Exception as exc:
                if not _is_duplicate(exc):
                    raise
                return False

        if state == "over":
            claim(LIMIT_WARN)  # после 100% предупреждение про 80% уже не нужно
            if claim(LIMIT_OVER):
                extra = spent - limit
                text = (i18n.L("🔴 «{0}»: лимит {1} {2} превышен на {3} {2}.", cat['name'], fmt(limit), limit_cur,
                               fmt(extra)) if extra > 0
                        else i18n.L("🔴 «{0}»: лимит {1} {2} исчерпан.", cat['name'], fmt(limit), limit_cur))
                sent.append(text)
        elif claim(LIMIT_WARN):
            last_day = calendar.monthrange(today.year, today.month)[1]
            days_left = last_day - today.day + 1
            per_day = ((limit - spent) / days_left).quantize(CENT) if days_left else Decimal(0)
            sent.append(i18n.L("⚠️ «{0}»: потрачено {1} {2} из {3} {2} ({4}%). До конца месяца {5} дн. — примерно "
                               "{6} {2} в день.", cat['name'], fmt(spent), limit_cur, fmt(limit), pct, days_left,
                               fmt(per_day)))
        if sent:
            import notifier
            for text in sent:
                notifier.notify_user(user_id, text)
            logger.info("Предупреждение о лимите: категория %s, %d%%", category_id, pct)
        return sent
    except Exception as exc:
        logger.warning("Проверка лимита не удалась: %s", type(exc).__name__)
        return []


def trend(user_id: str, months: int = 6) -> dict:
    """Доходы и расходы по месяцам за последние N месяцев (1–24), отдельно по валютам."""
    if not 1 <= int(months) <= 24:
        raise MoneyError(422, tr("Период — от 1 до 24 месяцев"), "bad_months")
    months = int(months)
    last = _month_start(today_for(user_id))
    first = _add_months(last, -(months - 1))
    keys = [_add_months(first, i).strftime("%Y-%m") for i in range(months)]
    rows = _period_rows(user_id, first, _add_months(last, 1))
    data: dict[str, dict[str, dict[str, Decimal]]] = {}
    for r in rows:
        point = data.setdefault(r["currency"], {}).setdefault(str(r["spent_on"])[:7],
                                                              {"income": Decimal(0), "expense": Decimal(0)})
        point[r["kind"]] += Decimal(str(r["amount"]))
    currencies = []
    for cur in CURRENCIES:
        if cur not in data:
            continue
        points = [{"month": k, "income": fmt(data[cur].get(k, {}).get("income", 0)),
                   "expense": fmt(data[cur].get(k, {}).get("expense", 0))} for k in keys]
        currencies.append({"currency": cur, "points": points})
    main_cur = default_currency(user_id)
    rates = rates_service.get_rates() if set(data) - {main_cur} else None
    main = None
    if rates_service.can_convert(rates, set(data), main_cur):
        main = {"currency": main_cur, "points": [{
            "month": k,
            "income": fmt(sum((rates_service.convert(data[c].get(k, {}).get("income", Decimal(0)), c, main_cur, rates)
                               for c in data), Decimal(0))),
            "expense": fmt(sum((rates_service.convert(data[c].get(k, {}).get("expense", Decimal(0)), c, main_cur, rates)
                                for c in data), Decimal(0))),
        } for k in keys]}
    return {"months": keys, "currencies": currencies, "main": main}


# ---------------------------------------------------------------- повторяющиеся платежи
# Частота: неделя, 2 недели, месяц, год. Правило создаётся вместе с первой операцией (форма траты,
# переключатель «Повторять»): anchor_date — её дата, следующие идут от неё. 31-е в коротком месяце
# и 29 февраля в невисокосном году дают последний день месяца, day_of_month при этом не меняется.

FREQUENCIES = ("weekly", "biweekly", "monthly", "yearly")


def _rec_out(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "amount": fmt(row["amount"]), "currency": row["currency"],
        "category_id": row["category_id"], "note": row.get("note"), "day_of_month": row["day_of_month"],
        "frequency": row.get("frequency") or "monthly",
        "anchor_date": str(row["anchor_date"])[:10] if row.get("anchor_date") else None,
        "next_run": str(row["next_run"])[:10], "active": row.get("active", True),
        "as_task": bool(row.get("as_task")),
    }


def _get_rec_row(rec_id: str, user_id: str) -> Optional[dict]:
    if not _is_uuid(rec_id):
        return None
    rows = supabase.table(REC_TABLE).select("*").eq("id", rec_id).eq("user_id", user_id).execute().data
    return rows[0] if rows else None


def _clean_day_of_month(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        dom = value
    elif isinstance(value, str) and value.strip().isdigit():
        dom = int(value.strip())
    else:
        dom = 0
    if not 1 <= dom <= 31:
        raise MoneyError(422, tr("День месяца — от 1 до 31"), "bad_day_of_month")
    return dom


def clean_frequency(value) -> str:
    freq = value or "monthly"
    if freq not in FREQUENCIES:
        raise MoneyError(422, tr("Повтор: каждую неделю, каждые 2 недели, каждый месяц или каждый год"), "bad_frequency")
    return freq


def advance(day: date, frequency: str, day_of_month: int) -> date:
    """Следующая дата платежа после day."""
    if frequency == "weekly":
        return day + timedelta(days=7)
    if frequency == "biweekly":
        return day + timedelta(days=14)
    if frequency == "yearly":
        return _clamped(day.year + 1, day.month, day_of_month)
    nxt = _add_months(_month_start(day), 1)
    return _clamped(nxt.year, nxt.month, day_of_month)


def next_occurrence(today: date, day_of_month: int) -> date:
    """Ежемесячный платёж без даты первой операции: ближайшее число не раньше сегодня."""
    this_month = _clamped(today.year, today.month, day_of_month)
    if this_month >= today:
        return this_month
    nxt = _add_months(_month_start(today), 1)
    return _clamped(nxt.year, nxt.month, day_of_month)


def _first_run_after(anchor: date, frequency: str, dom: int, today: date) -> date:
    """Первая дата после anchor, не раньше сегодня: правило не создаёт операций задним числом."""
    run = advance(anchor, frequency, dom)
    while run < today:
        run = advance(run, frequency, dom)
    return run


def list_recurring(user_id: str, kind: Optional[str] = None) -> list[dict]:
    query = supabase.table(REC_TABLE).select("*").eq("user_id", user_id)
    if kind:
        query = query.eq("kind", clean_kind(kind))
    rows = query.execute().data
    return [_rec_out(r) for r in sorted(rows, key=lambda r: (str(r["next_run"]), r.get("created_at") or ""))]


def create_recurring(user_id: str, data: dict) -> dict:
    """С anchor_date (дата уже записанной первой операции) следующий платёж — следующий период после неё.
    Без anchor_date — прежний ежемесячный платёж по day_of_month с ближайшего числа."""
    kind = clean_kind(data.get("kind"))
    frequency = clean_frequency(data.get("frequency"))
    today = today_for(user_id)
    anchor = parse_day(data["anchor_date"], user_id) if data.get("anchor_date") else None
    if anchor:
        dom = anchor.day
        next_run = _first_run_after(anchor, frequency, dom, today)
    else:
        dom = _clean_day_of_month(data.get("day_of_month"))
        if frequency != "monthly":
            raise MoneyError(422, tr("Для такого повтора нужна дата первого платежа"), "bad_anchor")
        next_run = next_occurrence(today, dom)
    row = {
        "user_id": user_id, "kind": kind,
        "amount": str(parse_amount(data.get("amount"))),
        "currency": clean_currency(data.get("currency")),
        "category_id": _own_category(user_id, data.get("category_id"), kind)["id"],
        "note": clean_note(data.get("note")),
        "day_of_month": dom,
        "frequency": frequency,
        "anchor_date": anchor.isoformat() if anchor else None,
        "next_run": next_run.isoformat(),
        "active": bool(data.get("active", True)),
    }
    if data.get("as_task"):  # до миграции 013 колонки нет — пишем её только когда нужна
        row["as_task"] = True
    rows = supabase.table(REC_TABLE).insert(row).execute().data
    if not rows:
        raise RuntimeError("Не удалось сохранить платёж")
    if rows[0].get("as_task"):
        ensure_payment_task(rows[0])
    return _rec_out(rows[0])


def update_recurring(rec_id: str, user_id: str, data: dict) -> Optional[dict]:
    """Меняет переданные поля. next_run — дата следующего платежа (не раньше сегодня): от неё же
    берётся число месяца. Смена частоты пересчитывает next_run от текущего, не раньше сегодня."""
    current = _get_rec_row(rec_id, user_id)
    if not current:
        return None
    changes = {}
    today = today_for(user_id)
    kind = current["kind"]
    if data.get("kind") is not None:
        kind = clean_kind(data["kind"])
        changes["kind"] = kind
    if data.get("amount") is not None:
        changes["amount"] = str(parse_amount(data["amount"]))
    if data.get("currency") is not None:
        changes["currency"] = clean_currency(data["currency"])
    if data.get("category_id") is not None or kind != current["kind"]:
        changes["category_id"] = _own_category(user_id, data.get("category_id") or current["category_id"], kind)["id"]
    if "note" in data:
        changes["note"] = clean_note(data["note"])
    frequency = current.get("frequency") or "monthly"
    dom = current["day_of_month"]
    if data.get("frequency") is not None and data["frequency"] != frequency:
        frequency = changes["frequency"] = clean_frequency(data["frequency"])
    if data.get("next_run") is not None:
        run = parse_day(data["next_run"], user_id)
        if run < today:
            raise MoneyError(422, tr("Следующий платёж — сегодня или позже"), "bad_next_run")
        dom = changes["day_of_month"] = run.day
        changes["next_run"] = run.isoformat()
    elif data.get("day_of_month") is not None:
        new_dom = _clean_day_of_month(data["day_of_month"])
        if new_dom != dom:
            dom = changes["day_of_month"] = new_dom
            changes["next_run"] = next_occurrence(today, dom).isoformat()
    elif "frequency" in changes:
        run = date.fromisoformat(str(current["next_run"])[:10])
        while run < today:
            run = advance(run, frequency, dom)
        changes["next_run"] = run.isoformat()
    if data.get("as_task") is not None and bool(data["as_task"]) != bool(current.get("as_task")):
        changes["as_task"] = bool(data["as_task"])
        if not changes["as_task"] and "next_run" not in changes:
            # снова списывать само — неоплаченный срок в прошлом задним числом не создаём
            run = date.fromisoformat(str(current["next_run"])[:10])
            while run < today:
                run = advance(run, frequency, dom)
            changes["next_run"] = run.isoformat()
    if data.get("active") is not None:
        changes["active"] = bool(data["active"])
        if changes["active"] and not current.get("active", True) and "next_run" not in changes:
            # включили обратно — пропущенное за время паузы не догоняем
            run = date.fromisoformat(str(current["next_run"])[:10])
            while run < today:
                run = advance(run, frequency, dom)
            changes["next_run"] = run.isoformat()
    if changes:
        supabase.table(REC_TABLE).update(changes).eq("id", rec_id).eq("user_id", user_id).execute()
    row = _get_rec_row(rec_id, user_id)
    if row and (row.get("as_task") or current.get("as_task")):
        if row.get("as_task") and row.get("active", True):
            _refresh_payment_task(row)
        else:
            _drop_payment_tasks(rec_id, user_id)
    return _rec_out(row) if row else None


def delete_recurring(rec_id: str, user_id: str) -> bool:
    """Останавливает повтор навсегда; уже созданные им операции остаются (recurring_id станет null)."""
    row = _get_rec_row(rec_id, user_id)
    if not row:
        return False
    if row.get("as_task"):
        _drop_payment_tasks(rec_id, user_id)  # неоплаченная задача уходит вместе с платежом
    supabase.table(REC_TABLE).delete().eq("id", rec_id).eq("user_id", user_id).execute()
    return True


# ---------------------------------------------------------------- платёж «по галочке» (миграция 013)
# money_recurring.as_task: вместо операции в срок — задача «💳 Жильё · 500 €» на next_run (23:59, «без времени»).
# Галочка (task_service.update_task → pay_from_task) записывает операцию сегодняшним днём и ставит задачу на
# следующий срок; удаление открытой задачи (skip_payment) — пропуск этого срока. Открытая задача у платежа одна
# (уникальный индекс tasks_money_recurring_open_idx); next_run двигается условным обновлением, поэтому две
# одновременные галочки не запишут операцию дважды.

CURRENCY_SIGNS = {"EUR": "€", "KZT": "₸", "USD": "$", "AZN": "₼", "TRY": "₺"}


def _payment_title(rec: dict) -> str:
    name = rec.get("note") or (get_category(rec["category_id"], rec["user_id"]) or {}).get("name") or "Платёж"
    amount = fmt(rec["amount"])
    amount = amount[:-3] if amount.endswith(".00") else amount.replace(".", ",")
    return f"💳 {name} · {amount} {CURRENCY_SIGNS.get(rec['currency'], rec['currency'])}"


def _open_payment_tasks(rec_id: str, user_id: str) -> list[dict]:
    return supabase.table("tasks").select("*").eq("user_id", user_id).eq("money_recurring_id", rec_id) \
        .neq("status", "Done").execute().data


def ensure_payment_task(rec: dict) -> Optional[dict]:
    """Открытая задача на ближайший срок платежа (если её ещё нет)."""
    if not rec.get("as_task") or not rec.get("active", True):
        return None
    user_id = rec["user_id"]
    if _open_payment_tasks(rec["id"], user_id):
        return None
    import categories_service
    import task_service
    row = {"user_id": user_id, "task": _payment_title(rec), "category": categories_service.fallback_name(user_id),
           "source": "Кошелёк", "deadline": f"{str(rec['next_run'])[:10]}T23:59:00", "priority": "Mid",
           "status": "To Do", "is_recurring": False, "tags": [], "money_recurring_id": rec["id"],
           "money_due": str(rec["next_run"])[:10]}
    try:
        task = supabase.table("tasks").insert(row).execute().data[0]
    except Exception as exc:
        if _is_duplicate(exc):  # задачу уже поставил параллельный запрос
            return None
        raise
    logger.info("Платёж %s: задача %s", rec["id"], task["id"])
    return task_service._sync_google_upsert(task, user_id)


def _refresh_payment_task(rec: dict) -> None:
    """Поменяли сумму, название или срок платежа — открытая задача следом (или ставится, если её нет)."""
    import task_service
    tasks = _open_payment_tasks(rec["id"], rec["user_id"])
    if not tasks:
        ensure_payment_task(rec)
        return
    changes = {"task": _payment_title(rec), "deadline": f"{str(rec['next_run'])[:10]}T23:59:00",
               "money_due": str(rec["next_run"])[:10]}
    for t in tasks:
        if t["task"] != changes["task"] or str(t.get("deadline") or "")[:16] != changes["deadline"][:16]:
            changes_t = {**changes, "ends_at": None, "updated_at": datetime.now(timezone.utc).isoformat()}
            row = supabase.table("tasks").update(changes_t).eq("id", t["id"]).eq("user_id", rec["user_id"]).execute().data
            if row:
                task_service._sync_google_upsert(row[0], rec["user_id"])


def _drop_payment_tasks(rec_id: str, user_id: str) -> None:
    import task_service
    for t in _open_payment_tasks(rec_id, user_id):
        task_service.delete_task(t["id"], user_id, payment_hook=False)


def _advance_claim(rec: dict, due_value) -> Optional[dict]:
    """Срок платежа due (срок задачи) → следующий, только если платёж всё ещё ждёт именно его.
    None — этот срок уже оплачен или пропущен (повторная или одновременная галочка)."""
    if not due_value or str(due_value)[:10] != str(rec["next_run"])[:10]:
        return None
    due = date.fromisoformat(str(rec["next_run"])[:10])
    nxt = advance(due, rec.get("frequency") or "monthly", rec["day_of_month"])
    claimed = supabase.table(REC_TABLE).update({"next_run": nxt.isoformat()}).eq("id", rec["id"]) \
        .eq("user_id", rec["user_id"]).eq("next_run", str(rec["next_run"])[:10]).execute().data
    return {**rec, "next_run": nxt.isoformat(), "_due": due} if claimed else None


def _record_payment(rec: dict, moved: dict, user_id: str) -> dict:
    """Операция по платежу сегодняшним днём (если сегодня у правила уже есть операция — датой срока)."""
    row = {"user_id": user_id, "kind": rec["kind"], "amount": str(rec["amount"]), "currency": rec["currency"],
           "category_id": rec["category_id"], "note": rec.get("note"), "spent_on": today_for(user_id).isoformat(),
           "source": "recurring", "recurring_id": rec["id"]}
    try:
        tx = supabase.table(TX_TABLE).insert(row).execute().data[0]
    except Exception as exc:
        if not _is_duplicate(exc):
            raise
        tx = supabase.table(TX_TABLE).insert({**row, "spent_on": moved["_due"].isoformat()}).execute().data[0]
    if rec["kind"] == "expense":
        check_limit_alerts(user_id, rec["category_id"], tx["spent_on"])
    return tx


def pay_from_task(task: dict, user_id: str) -> Optional[dict]:
    """Галочка на задаче платежа: операция сегодняшним днём, задача на следующий срок. Возвращает операцию."""
    rec = _get_rec_row(task.get("money_recurring_id") or "", user_id)
    if not rec:
        return None
    moved = _advance_claim(rec, task.get("money_due"))
    if not moved:
        return None
    tx = _record_payment(rec, moved, user_id)
    logger.info("Платёж %s оплачен галочкой: операция %s", rec["id"], tx["id"])
    moved.pop("_due")
    ensure_payment_task(moved)
    return _tx_out(tx)


def pay_now(rec_id: str, user_id: str) -> Optional[dict]:
    """Галочка у платежа в кошельке (любого, решение владельца 11 октября 2026): оплата записана сегодня, срок —
    следующий. У платежа «по галочке» это то же, что галочка на его задаче. Платёж на паузе — снова в работе."""
    rec = _get_rec_row(rec_id, user_id)
    if not rec:
        return None
    if not rec.get("active", True):
        supabase.table(REC_TABLE).update({"active": True}).eq("id", rec_id).eq("user_id", user_id).execute()
        rec["active"] = True
    open_tasks = _open_payment_tasks(rec_id, user_id) if rec.get("as_task") else []
    if open_tasks:
        import task_service
        from models import TaskUpdate
        task_service.update_task(open_tasks[0]["id"], TaskUpdate(status="Done"), user_id)
    else:
        moved = _advance_claim(rec, rec["next_run"])
        if moved:
            tx = _record_payment(rec, moved, user_id)
            logger.info("Платёж %s оплачен в кошельке: операция %s", rec_id, tx["id"])
            moved.pop("_due")
            ensure_payment_task(moved)
    row = _get_rec_row(rec_id, user_id)
    return _rec_out(row) if row else None


def skip_payment(task: dict, user_id: str) -> None:
    """Удалили неоплаченную задачу платежа — этот срок пропущен, ставится следующий."""
    rec = _get_rec_row(task.get("money_recurring_id") or "", user_id)
    if not rec or not rec.get("as_task"):
        return
    moved = _advance_claim(rec, task.get("money_due"))
    if moved:
        moved.pop("_due")
        ensure_payment_task(moved)


def run_recurring(today_of=None, utc_today: Optional[date] = None) -> list[tuple[str, str]]:
    """Создаёт операции по активным платежам, у которых next_run не позже сегодняшней даты пользователя.
    Догоняет пропущенные периоды (не больше 12 за прогон). Повторный прогон ничего не дублирует
    (уникальный индекс (recurring_id, spent_on)). Сбой одного платежа не останавливает остальные.
    Возвращает сообщения для пользователей: [(user_id, текст)].
    today_of и utc_today подменяются только в проверках (scripts/money_check.py)."""
    today_of = today_of or today_for
    horizon = ((utc_today or datetime.now(timezone.utc).date()) + timedelta(days=1)).isoformat()  # пояса впереди UTC максимум на сутки
    due = _fetch_all(lambda: supabase.table(REC_TABLE).select("*").eq("active", True).lte("next_run", horizon))
    messages, today_cache = [], {}
    for rec in due:
        try:
            user_id = rec["user_id"]
            if user_id not in today_cache:
                today_cache[user_id] = today_of(user_id)
            today = today_cache[user_id]
            if rec.get("as_task"):  # списывается галочкой на задаче, сам не создаётся
                ensure_payment_task(rec)
                continue
            frequency = rec.get("frequency") or "monthly"
            run_day = date.fromisoformat(str(rec["next_run"])[:10])
            created = 0
            for _ in range(CATCH_UP_MAX):
                if run_day > today:
                    break
                try:
                    supabase.table(TX_TABLE).insert({
                        "user_id": user_id, "kind": rec["kind"], "amount": str(rec["amount"]),
                        "currency": rec["currency"], "category_id": rec["category_id"], "note": rec.get("note"),
                        "spent_on": run_day.isoformat(), "source": "recurring", "recurring_id": rec["id"],
                    }).execute()
                    created += 1
                except Exception as exc:
                    if not _is_duplicate(exc):
                        raise
                run_day = advance(run_day, frequency, rec["day_of_month"])
            supabase.table(REC_TABLE).update({"next_run": run_day.isoformat()}).eq("id", rec["id"]).eq(
                "user_id", user_id).execute()
            if created and rec["kind"] == "expense":
                check_limit_alerts(user_id, rec["category_id"], today.isoformat())
            if created:
                name = rec.get("note") or (get_category(rec["category_id"], user_id) or {}).get("name", "Платёж")
                text = (i18n.L("Добавлен платёж: {0} · {1} {2} (×{3}, за пропущенные периоды)", name,
                               fmt(rec['amount']), rec['currency'], created) if created > 1
                        else i18n.L("Добавлен платёж: {0} · {1} {2}", name, fmt(rec['amount']), rec['currency']))
                messages.append((user_id, text))
            logger.info("Повторяющийся платёж %s: создано операций %d", rec["id"], created)
        except Exception as exc:
            logger.warning("Повторяющийся платёж %s не обработан: %s", rec.get("id"), type(exc).__name__)
    return messages


# ---------------------------------------------------------------- текст → деньги (бот и /money/parse)

CURRENCY_MARKERS = {
    "EUR": ("€", "евро", "eur", "euro"),
    "KZT": ("₸", "тенге", "тг", "kzt"),
    "USD": ("$", "доллар", "доллара", "долларов", "долл", "бакс", "бакса", "баксов", "usd"),
    "AZN": ("₼", "манат", "маната", "манатов", "azn"),
    "TRY": ("₺", "лира", "лиры", "лир", "лиру", "try"),
}
_MARKER_RE = "|".join(sorted((re.escape(m) for ms in CURRENCY_MARKERS.values() for m in ms), key=len, reverse=True))
_NUMBER_RE = r"\d[\d\s ]*(?:[.,]\d+)?\s*(?:к|k|тыс\.?)?"
# число рядом с маркером валюты (в любом порядке), маркер не часть другого слова
_MONEY_RE = re.compile(
    rf"(?:{_NUMBER_RE}\s*(?:{_MARKER_RE})(?![а-яёa-z]))|(?:(?<![а-яёa-z])(?:{_MARKER_RE})\s*\d)",
    re.IGNORECASE,
)
_KIND_WORDS = {"расход": "expense", "доход": "income"}


def looks_like_money(text: str) -> bool:
    """Без вызова модели: есть ли в тексте число рядом с маркером валюты («3 евро», «€12», «5к тенге»)."""
    return bool(_MONEY_RE.search(text or ""))


def money_mode(text: str) -> tuple[bool, Optional[str], str]:
    """Решение «деньги или задача» для сообщения без команды.
    Первое слово «расход»/«доход» включает денежный режим и задаёт тип; иначе — looks_like_money.
    Возвращает (деньги?, тип или None, текст без слова-переключателя)."""
    stripped = (text or "").strip()
    first, _, rest = stripped.partition(" ")
    kind = _KIND_WORDS.get(first.lower().strip(":,.—-"))
    if kind:
        return True, kind, rest.strip()
    return looks_like_money(stripped), None, stripped


def drafts_from_text(user_id: str, text: str, force_kind: Optional[str] = None) -> list[dict]:
    """Черновики операций из текста через модель, без сохранения.
    Сумму модель не выдумывает: без цифр в тексте модель не вызывается вовсе.
    Ответ модели сверяется: сумма — Decimal из строки, валюта и тип из допустимых,
    категория — существующая своего типа (иначе «Прочее» или первая), дата — в пределах ± 5 лет."""
    from groq_client import parse_money_text

    if not re.search(r"\d", text or ""):
        return []
    today = today_for(user_id)
    default_cur = default_currency(user_id)
    names = {kind: [c["name"] for c in list_categories(user_id, kind)] for kind in KINDS}
    raw_items = parse_money_text(text, names, today, default_cur)
    drafts = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            amount = parse_amount(item.get("amount"))
        except MoneyError:
            continue  # без суммы операции нет
        kind = force_kind or (item.get("kind") if item.get("kind") in KINDS else "expense")
        currency = str(item.get("currency") or "").upper()
        if currency not in CURRENCIES:
            currency = default_cur
        try:
            day = parse_day(item.get("date") or today, user_id)
        except MoneyError:
            day = today
        cat = resolve_category(user_id, kind, item.get("category"))
        note = (str(item.get("note") or "").strip() or None)
        drafts.append({"kind": kind, "amount": fmt(amount), "currency": currency, "category_id": cat["id"],
                       "category": cat["name"], "note": note[:NOTE_MAX] if note else None,
                       "spent_on": day.isoformat()})
    return drafts


# ---------------------------------------------------------------- Apple Pay (автоматизация «Транзакция» в «Командах»)
# iPhone при каждой оплате картой из Wallet присылает сумму текстом («€23,40», «4 000 ₸», «KZT 4,000.00»),
# магазин и карту. Категория: память магазинов (money_merchant_rules, учится на исправлениях пользователя),
# иначе подбор моделью по названию магазина (без суммы), иначе «Прочее». Повтор той же покупки в течение
# двух минут не записывается второй раз. Возвраты (сумма со знаком минус) пока не записываются.

APPLEPAY_DEDUP = timedelta(minutes=2)
MERCHANT_TABLE = "money_merchant_rules"
_CURRENCY_TOKENS = [("тенге", "KZT"), ("euro", "EUR"), ("евро", "EUR"), ("kzt", "KZT"), ("eur", "EUR"),
                    ("тг", "KZT"), ("€", "EUR"), ("₸", "KZT"), ("usd", "USD"), ("us$", "USD"), ("$", "USD"),
                    ("azn", "AZN"), ("₼", "AZN"), ("try", "TRY"), ("₺", "TRY"), ("tl", "TRY")]
_OTHER_CURRENCY = re.compile(r"[£¥₽]|\b(gbp|rub|chf|jpy|cny|aed)\b", re.IGNORECASE)


def parse_applepay_amount(text, default_cur: str) -> tuple[Decimal, str, bool]:
    """Сумма из «Команд» в любом виде → (Decimal, валюта, это возврат?). Валюта не из CURRENCIES — 422."""
    raw = str(text or "").strip()
    low = raw.lower()
    currency = next((cur for token, cur in _CURRENCY_TOKENS if token in low), None)
    if currency is None and _OTHER_CURRENCY.search(low):
        raise MoneyError(422, tr('Валюта этой покупки не поддерживается (только {0})', ', '.join(CURRENCIES)), "bad_currency")
    refund = "-" in raw or "−" in raw
    num = re.sub(r"[^\d.,]", "", raw)
    if not re.search(r"\d", num):
        raise MoneyError(422, tr("Не нашёл сумму покупки"), "bad_amount")
    if "," in num and "." in num:  # десятичный знак — тот, что правее
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    else:
        sep = "," if "," in num else "." if "." in num else None
        if sep:
            head, _, tail = num.rpartition(sep)
            num = (head.replace(sep, "") + "." + tail) if len(tail) in (1, 2) else num.replace(sep, "")
    return parse_amount(num), currency or default_cur, refund


def merchant_key(merchant: Optional[str]) -> Optional[str]:
    """«ESSELUNGA SpA 123» → «esselunga spa»: без цифр и знаков, регистр и ё не важны."""
    text = (merchant or "").lower().replace("ё", "е")
    text = re.sub(r"[.'’`-]", "", text)  # «S.p.A.» = «SpA», «McDonald's» = «McDonalds»
    text = re.sub(r"[^a-zа-я ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:60] or None


def remember_merchant(user_id: str, merchant: Optional[str], category_id: str) -> None:
    """Запомнить, что покупки в этом магазине — в этой категории (своей, расходной)."""
    key = merchant_key(merchant)
    cat = get_category(category_id, user_id)
    if not key or not cat or cat["kind"] != "expense":
        return
    existing = supabase.table(MERCHANT_TABLE).select("user_id").eq("user_id", user_id).eq("merchant_key", key).execute().data
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        supabase.table(MERCHANT_TABLE).update({"category_id": cat["id"], "updated_at": now}).eq(
            "user_id", user_id).eq("merchant_key", key).execute()
    else:
        supabase.table(MERCHANT_TABLE).insert({"user_id": user_id, "merchant_key": key, "category_id": cat["id"]}).execute()


def category_for_merchant(user_id: str, merchant: Optional[str]) -> tuple[dict, str]:
    """(категория, откуда: 'memory' | 'model' | 'fallback')."""
    key = merchant_key(merchant)
    if key:
        rows = supabase.table(MERCHANT_TABLE).select("category_id").eq("user_id", user_id).eq("merchant_key", key).execute().data
        cat = get_category(rows[0]["category_id"], user_id) if rows else None
        if cat and cat["kind"] == "expense":
            return cat, "memory"
        try:
            from groq_client import classify_merchant

            names = [c["name"] for c in list_categories(user_id, "expense")]
            guess = classify_merchant(merchant, names)
            if guess and guess.strip().lower() in {n.lower() for n in names}:
                return resolve_category(user_id, "expense", guess), "model"
        except Exception as exc:
            logger.warning("Категория магазина не подобрана: %s", type(exc).__name__)
    return resolve_category(user_id, "expense", None), "fallback"


def record_applepay(user_id: str, amount_text, merchant: Optional[str]) -> tuple[dict, bool, str]:
    """Покупка из Apple Pay → операция. Возвращает (операция, это повтор?, откуда категория)."""
    amount, currency, refund = parse_applepay_amount(amount_text, default_currency(user_id))
    if refund:
        raise MoneyError(422, tr("Возвраты пока не записываются"), "refund")
    note = (merchant or "").strip()[:NOTE_MAX] or "Apple Pay"
    cutoff = (datetime.now(timezone.utc) - APPLEPAY_DEDUP).isoformat()
    recent = supabase.table(TX_TABLE).select("*").eq("user_id", user_id).eq("source", "applepay").gte(
        "created_at", cutoff).execute().data
    for row in recent:
        if row.get("note") == note and row["currency"] == currency and Decimal(str(row["amount"])) == amount:
            return _tx_out(row), True, "duplicate"
    cat, how = category_for_merchant(user_id, note)
    tx = create_transaction(user_id, {"kind": "expense", "amount": str(amount), "currency": currency,
                                      "category_id": cat["id"], "note": note}, source="applepay")
    return tx, False, how


# ---------------------------------------------------------------- аналитика за период и месячный отчёт

def analytics(user_id: str, date_from=None, date_to=None) -> dict:
    """Итоги за любой период в основной валюте: расходы, доходы, баланс, расход в среднем за день, категории
    (с изменением к предыдущему такому же периоду), где тратится больше всего (по магазинам и заметкам)
    и самые крупные траты. По умолчанию — текущий месяц."""
    today = today_for(user_id)
    start = parse_day(date_from, user_id) if date_from else _month_start(today)
    end = parse_day(date_to, user_id) if date_to else _add_months(_month_start(today), 1) - timedelta(days=1)
    if end < start:
        raise MoneyError(422, tr("Конец периода раньше начала"), "bad_period")
    if (end - start).days > 3 * 366:
        raise MoneyError(422, tr("Период — не длиннее трёх лет"), "bad_period")
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    rows = _fetch_all(lambda: supabase.table(TX_TABLE).select("kind,amount,currency,category_id,spent_on,note")
                      .eq("user_id", user_id).gte("spent_on", prev_start.isoformat()).lte("spent_on", end.isoformat()))
    main = default_currency(user_id)
    rates = rates_service.get_rates() if {r["currency"] for r in rows} - {main} else None
    if not rates_service.can_convert(rates, {r["currency"] for r in rows}, main):
        raise MoneyError(503, tr("Курс валют сейчас недоступен, попробуйте позже"), "no_rate")
    cats = {c["id"]: c for c in list_categories(user_id)}
    cur_rows = [r for r in rows if str(r["spent_on"])[:10] >= start.isoformat()]
    prev_rows = [r for r in rows if str(r["spent_on"])[:10] < start.isoformat()]

    def conv(r):
        return rates_service.convert(Decimal(str(r["amount"])), r["currency"], main, rates)

    def sums(selected):
        out = {"income": Decimal(0), "expense": Decimal(0), "by_cat": {}, "approx": set()}
        for r in selected:
            amount = conv(r)
            out[r["kind"]] += amount
            if r["currency"] != main:  # где был пересчёт — там клиент ставит «≈»
                out["approx"].update({r["kind"], ("cat", r["category_id"])})
            if r["kind"] == "expense":
                out["by_cat"][r["category_id"]] = out["by_cat"].get(r["category_id"], Decimal(0)) + amount
        return out

    now, before = sums(cur_rows), sums(prev_rows)

    def change(a: Decimal, b: Decimal) -> Optional[int]:
        return int(((a - b) * 100 / b).to_integral_value()) if b else None

    by_category = []
    for item in _breakdown(cats, now["by_cat"], now["expense"]):
        prev = before["by_cat"].get(item["category_id"], Decimal(0))
        by_category.append({**item, "change_pct": change(Decimal(item["amount"]), prev),
                            "approx": ("cat", item["category_id"]) in now["approx"]})
    places = {}
    for r in cur_rows:
        if r["kind"] != "expense":
            continue
        key = merchant_key(r.get("note")) or f"cat:{r['category_id']}"
        label = (r.get("note") or cats.get(r["category_id"], {}).get("name", "?")).strip()
        place = places.setdefault(key, {"name": label, "count": 0, "amount": Decimal(0), "approx": False})
        place["count"] += 1
        place["amount"] += conv(r)
        place["approx"] = place["approx"] or r["currency"] != main
    top_places = sorted(places.values(), key=lambda p: p["amount"], reverse=True)[:5]
    largest = sorted((r for r in cur_rows if r["kind"] == "expense"), key=conv, reverse=True)[:5]
    return {
        "from": start.isoformat(), "to": end.isoformat(), "days": days, "currency": main,
        "approx": bool(now["approx"]), "expense_approx": "expense" in now["approx"], "income_approx": "income" in now["approx"],
        "expense": fmt(now["expense"]), "income": fmt(now["income"]), "balance": fmt(now["income"] - now["expense"]),
        "avg_per_day": fmt((now["expense"] / days).quantize(CENT)),
        "previous": {"from": prev_start.isoformat(), "to": prev_end.isoformat(),
                     "expense": fmt(before["expense"]), "income": fmt(before["income"])},
        "expense_change_pct": change(now["expense"], before["expense"]),
        "by_category": by_category,
        "top_places": [{"name": p["name"], "count": p["count"], "amount": fmt(p["amount"]), "approx": p["approx"]}
                       for p in top_places],
        "largest": [{"name": (r.get("note") or cats.get(r["category_id"], {}).get("name", "?")), "amount": fmt(conv(r)),
                     "spent_on": str(r["spent_on"])[:10], "approx": r["currency"] != main} for r in largest],
    }




def monthly_report_text(user_id: str, month_start: date) -> Optional[str]:
    """«📊 Итоги сентября» — за указанный (прошедший) месяц. None — если операций в нём не было."""
    end = _add_months(month_start, 1) - timedelta(days=1)
    try:
        a = analytics(user_id, month_start.isoformat(), end.isoformat())
    except MoneyError:
        return None
    if a["expense"] == "0.00" and a["income"] == "0.00":
        return None
    cur = a["currency"]
    ap = "≈ " if a["expense_approx"] else ""
    ai = "≈ " if a["income_approx"] else ""
    name = i18n.month_name(month_start.month)
    name = name if i18n.current() == "en" else name.lower()  # «Итоги: октябрь 2026»
    lines = [tr("📊 Итоги: {0} {1}", name, month_start.year)]
    change = a["expense_change_pct"]
    lines.append(tr("Потрачено {0}{1} {2}", ap, a['expense'], cur) +
                 (tr(" ({0}{1}% к прошлому месяцу)", '+' if change > 0 else '', change) if change is not None else ""))
    ab = "≈ " if a["approx"] else ""
    lines.append(tr("Доходы {0}{1} {2} · баланс {3}{4}{5} {2}", ai, a['income'], cur, ab,
                    '+' if not a['balance'].startswith('-') else '', a['balance']))
    if a["by_category"]:
        lines.append(tr("\nБольше всего:"))
        lines += [f"  {c['name']}: {c['amount']} {cur} ({c['share']}%)" for c in a["by_category"][:3]]
    if a["largest"]:
        lines.append(tr("Крупнейшие траты: ") + ", ".join(f"{x['name']} {x['amount']} {cur}" for x in a["largest"][:3]))
    try:
        s = summary(user_id, month_start.strftime("%Y-%m"))
        limits = (s.get("main") or {}).get("limits") or {}
        total = sum(limits.values())
        if total:
            over = [c["name"] for c in s["main"]["by_category"] if (c.get("limit") or {}).get("state") == "over"]
            lines.append(tr("Лимиты: превышено {0} из {1}", len(over), total) +
                         (f" ({', '.join(over)})" if over else tr(" — все в норме 👍")))
    except Exception as exc:
        logger.warning("Лимиты для отчёта не посчитаны: %s", type(exc).__name__)
    return "\n".join(lines)


def claim_monthly_report(user_id: str, month_key: str) -> bool:
    """Отчёт за месяц — один раз (строка в money_report_log вставляется до отправки)."""
    try:
        supabase.table("money_report_log").insert({"user_id": user_id, "month": month_key}).execute()
        return True
    except Exception as exc:
        if not _is_duplicate(exc):
            logger.warning("money_report_log: вставка не удалась (%s)", type(exc).__name__)
        return False
