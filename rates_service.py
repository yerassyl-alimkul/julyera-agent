"""
Курс валют для пересчёта в основную валюту пользователя — только для показа итогов.

Источник — официальные курсы Национального банка Казахстана (тенге за единицу валюты), бесплатно и без ключа.
Пересчёт идёт по сегодняшнему курсу (решение владельца 8 октября 2026): сами операции хранятся в своей валюте
и не меняются. Курс кэшируется в памяти на 6 часов; если Нацбанк недоступен — берётся последний полученный,
а если курса нет вовсе, get_rates() возвращает None и итоги показываются по валютам раздельно.
"""
import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RATES_URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={day}"
CACHE_SECONDS = 6 * 3600
CENT = Decimal("0.01")
_cache: dict = {"at": 0.0, "rates": None}


def _fetch(day: date) -> Optional[dict]:
    resp = httpx.get(RATES_URL.format(day=day.strftime("%d.%m.%Y")), timeout=5)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    kzt_per = {"KZT": Decimal(1)}
    for item in root.iter("item"):
        code = (item.findtext("title") or "").strip().upper()
        value, quant = item.findtext("description"), item.findtext("quant") or "1"
        if code and value:
            kzt_per[code] = Decimal(value.strip()) / Decimal(quant.strip())
    if "EUR" not in kzt_per:
        return None
    published = root.findtext("date") or day.strftime("%d.%m.%Y")
    return {"date": datetime.strptime(published.strip(), "%d.%m.%Y").date().isoformat(), "kzt_per": kzt_per}


def get_rates() -> Optional[dict]:
    """{"date": "YYYY-MM-DD", "kzt_per": {"EUR": Decimal, "KZT": 1, ...}} или None, если курса нет."""
    if _cache["rates"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["rates"]
    try:
        rates = _fetch(date.today())
        if rates:
            _cache.update(at=time.time(), rates=rates)
    except Exception as exc:
        logger.warning("Курс Нацбанка не получен: %s", type(exc).__name__)
    return _cache["rates"]


def can_convert(rates: Optional[dict], currencies, to: str) -> bool:
    needed = set(currencies) | {to}
    return needed <= {to} or bool(rates and needed <= set(rates["kzt_per"]))


def convert(amount: Decimal, from_cur: str, to_cur: str, rates: Optional[dict]) -> Decimal:
    """Сумма в другой валюте по курсу через тенге, с округлением до копеек."""
    if from_cur == to_cur:
        return Decimal(amount).quantize(CENT)
    k = rates["kzt_per"]
    return (Decimal(amount) * k[from_cur] / k[to_cur]).quantize(CENT, rounding=ROUND_HALF_UP)


def rate_info(rates: Optional[dict], main: str) -> Optional[dict]:
    """Для подписи «Курс Нацбанка РК на 8 октября: 1 € = 500,35 ₸» — сколько тенге за единицу основной валюты."""
    if not rates:
        return None
    other = "EUR" if main == "KZT" else main
    return {"date": rates["date"], "currency": other, "kzt": str(rates["kzt_per"][other].quantize(CENT))}
