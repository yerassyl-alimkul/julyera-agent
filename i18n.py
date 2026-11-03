"""
Языки сервера: тексты бота, сводок, напоминаний, выписки PDF и сообщения об ошибках API (решение владельца
3 ноября 2026). Языки — ru (исходный), en, kk; словарь — locales/server.json, ключ — русская фраза как есть.

Язык берётся из контекста (ContextVar): бот ставит его по пользователю (`use_for`), рассылки — на каждого
получателя (`for_user`), API — из заголовка X-Lang приложения (LangMiddleware в main.py).
    tr('Найдено: {0}', n)          — перевод с подстановкой
    tn(n, 'задача|задачи|задач')   — форма слова для числа
    L('…{0}…', x)                  — «ленивый» текст: переводится на язык получателя при отправке (notifier)
Пропуски перевода: python scripts/i18n_server_keys.py
"""
import contextlib
import contextvars
import json
import os
import re

LANGS = ("ru", "en", "kk")
_current = contextvars.ContextVar("julyera_lang", default="ru")

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales", "server.json")
try:
    with open(_PATH, encoding="utf-8") as fh:
        DICTS = json.load(fh)
except (OSError, ValueError):
    DICTS = {}

MONTHS_GEN = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "kk": ["қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым", "шілде", "тамыз", "қыркүйек", "қазан",
           "қараша", "желтоқсан"],
}
MONTHS_SHORT = {
    "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "kk": ["қаң", "ақп", "нау", "сәу", "мам", "мау", "шіл", "там", "қыр", "қаз", "қар", "жел"],
}
MONTHS_NOMINATIVE = {
    "ru": ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
           "Ноябрь", "Декабрь"],
    "en": MONTHS_GEN["en"],
    "kk": ["Қаңтар", "Ақпан", "Наурыз", "Сәуір", "Мамыр", "Маусым", "Шілде", "Тамыз", "Қыркүйек", "Қазан",
           "Қараша", "Желтоқсан"],
}
WEEKDAYS_SHORT = {  # 0 = понедельник
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сн", "Жс"],
}


def norm(code) -> str | None:
    code = str(code or "").lower()[:2]
    return code if code in LANGS else None


def current() -> str:
    return _current.get()


def use(code) -> str:
    """Поставить язык текущего контекста (на время обработки одного сообщения или запроса)."""
    lang = norm(code) or "ru"
    _current.set(lang)
    return lang


def user_lang(user: dict | None, fallback=None) -> str:
    """Язык пользователя: выбранный в приложении (users.language), иначе язык Telegram, иначе русский."""
    return norm((user or {}).get("language")) or norm(fallback) or "ru"


def use_for(user: dict | None, fallback=None) -> str:
    return use(user_lang(user, fallback))


@contextlib.contextmanager
def for_user(user: dict | None):
    """Временно говорить на языке этого пользователя (рассылки по многим людям)."""
    token = _current.set(user_lang(user))
    try:
        yield
    finally:
        _current.reset(token)


def _fill(s: str, args) -> str:
    if not args:
        return s
    return re.sub(r"\{(\d)\}", lambda m: "" if int(m.group(1)) >= len(args) or args[int(m.group(1))] is None
                  else str(args[int(m.group(1))]), s)


def lookup(s: str, lang: str | None = None) -> str:
    lang = lang or current()
    if lang == "ru":
        return s
    d = DICTS.get(lang) or {}
    if s in d:
        return d[s]
    key = s.strip()  # «\nЗавтра:» — ключ без пробелов и переводов строк по краям, они сохраняются
    if key != s and key in d:
        return s.replace(key, d[key], 1)
    return s


def tr(s: str, *args) -> str:
    return _fill(lookup(s), args)


def tn(n: int, forms: str) -> str:
    lang = current()
    d = DICTS.get(lang) or {}
    if lang != "ru" and forms in d:
        f = d[forms].split("|")
        if lang == "en":
            return f[0] if abs(n) == 1 else (f[1] if len(f) > 1 else f[0])
        return f[0]
    f = forms.split("|")
    a = abs(n) % 100
    b = a % 10
    i = 2 if 10 < a < 20 else 0 if b == 1 else 1 if 2 <= b <= 4 else 2
    return f[i] if i < len(f) else f[0]


def month_gen(m: int) -> str:
    return MONTHS_GEN[current()][m - 1]


def month_short(m: int) -> str:
    return MONTHS_SHORT[current()][m - 1]


def month_name(m: int) -> str:
    return MONTHS_NOMINATIVE[current()][m - 1]


def weekday_short(i: int) -> str:
    return WEEKDAYS_SHORT[current()][i]


def day_month(d) -> str:
    """«5 октября» / «5 October» / «5 қазан»."""
    return f"{d.day} {month_gen(d.month)}"


class L(str):
    """Текст, который переводится в момент отправки — на язык получателя (notifier.notify_user, рассылки).
    Сам по себе это обычная строка с русским текстом (для логики, проверок и старого кода)."""

    def __new__(cls, s: str, *args):
        obj = super().__new__(cls, _fill(s, [str(a) for a in args]))
        obj.key, obj.args = s, args
        return obj

    def render(self) -> str:
        return _fill(lookup(self.key), [a.render() if isinstance(a, L) else a for a in self.args])


def render(text) -> str:
    return text.render() if isinstance(text, L) else text
