"""
Поиск адреса для поля «Место» (migration 014): OpenStreetMap Nominatim, без ключа Google.
Правила Nominatim: не чаще запроса в секунду и свой User-Agent — запросы идут через сервер по очереди, ответы
кэшируются в памяти на сутки. Карта и «Открыть в Google Maps» в приложении строятся по тексту адреса.
Текст запроса не пишется в лог.
"""
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "JulYera-Agent/1.0 (+https://julyera-agent.onrender.com)"
CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()
_last = [0.0]


def _item(row: dict) -> dict:
    full = row.get("display_name") or ""
    name = row.get("name") or full.split(",")[0]
    return {"name": name.strip()[:120], "address": full.strip()[:200]}


def search(query: str, lang: str = "ru") -> list[dict]:
    q = " ".join((query or "").split())[:120]
    if len(q) < 3:
        return []
    key = f"{lang}:{q.lower()}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    with _lock:  # не чаще раза в секунду на весь сервер
        wait = 1.0 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            resp = httpx.get(URL, params={"q": q, "format": "jsonv2", "limit": 5, "accept-language": lang},
                             headers={"User-Agent": USER_AGENT}, timeout=8)
            resp.raise_for_status()
            items = [_item(r) for r in resp.json()]
        except Exception as exc:
            logger.warning("Поиск адреса не удался: %s", type(exc).__name__)
            raise
    _cache[key] = (time.time(), items)
    if len(_cache) > 2000:
        _cache.clear()
    return items
