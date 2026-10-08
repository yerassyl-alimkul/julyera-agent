"""
Обёртка над Groq API: превращает произвольный текст в список
структурированных задач по схеме, которую использует наш API.
"""
import json
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY должен быть задан в .env файле.")

_client = Groq(api_key=GROQ_API_KEY)

MODEL = "openai/gpt-oss-120b"

# «Сегодня» для разбора «завтра», «в пятницу» и приоритета считается по поясу пользователя
# (users.timezone, передаёт task_service), а не по часам сервера (UTC): иначе после полуночи
# «завтра» сдвигается на день. Это значение — запасное, если пояс не передан.
USER_TIMEZONE = ZoneInfo("Europe/Rome")

SYSTEM_PROMPT = """Ты — ассистент, который превращает произвольный текст (учебный материал,
письмо, список дел) в структурированные задачи.

Правила:
- Извлекай только конкретные, выполнимые действия. Игнорируй общий информационный текст.
- Поле category — строго одно из категорий пользователя: {categories}.
  Выбирай по смыслу задачи; если ни одна не подходит явно — первую из списка.
- Поле deadline включает И дату, И время в формате "YYYY-MM-DDTHH:MM:SS".
  Если время суток явно упомянуто в тексте (например "в 18:00", "к обеду", "утром") —
  используй его (примерно: "утром" → 09:00, "днём" → 14:00, "вечером" → 19:00,
  "к обеду" → 13:00, "ночью" → 23:00). Если время НЕ упомянуто вообще — ставь 23:59:00
  (конец дня по умолчанию).
- Если дедлайн явно не указан, но есть косвенное упоминание (например "экзамен в декабре"),
  поставь дедлайн за 7 дней до события, 23:59:00, и опиши это в note.
- Поле priority определяй по близости дедлайна относительно сегодняшней даты ({today}):
  меньше 3 дней — "High", меньше 14 дней — "Mid", иначе — "Low".
  Если дедлайна нет вообще — ставь "Mid".
- is_recurring: true, только если из текста явно следует повторяемость
  (например "каждую неделю", "ежедневно"). Тогда recurrence_pattern — одно из
  "daily", "weekly", "monthly". Иначе is_recurring: false, recurrence_pattern: null.
- Поле tags — список строк, только явные хэштеги из текста (например "#экзамен"
  превращается в "экзамен", без символа #). Если хэштегов в тексте нет — пустой
  список []. Не придумывай теги сам, только то, что явно написано через #.
- Верни ТОЛЬКО валидный JSON-объект вида {{"tasks": [...]}}, без пояснений и без markdown.

Формат каждого элемента массива "tasks":
{{
  "task": "строка",
  "category": "одно из названий категорий пользователя",
  "source": "строка или null",
  "deadline": "YYYY-MM-DDTHH:MM:SS или null",
  "priority": "High | Mid | Low",
  "note": "строка или пусто",
  "is_recurring": true | false,
  "recurrence_pattern": "daily | weekly | monthly | null",
  "tags": ["строка", ...]
}}"""


def parse_text_to_tasks(raw_text: str, categories: list[str], source_hint: Optional[str] = None,
                        timezone: Optional[str] = None) -> list[dict]:
    """
    Отправляет сырой текст в Groq и возвращает список словарей,
    готовых к вставке в таблицу tasks (без id/created_at/updated_at).
    categories — названия категорий пользователя: модель выбирает только из них,
    а task_service всё равно сверяет ответ и подставляет запасную категорию.
    timezone — пояс пользователя (users.timezone), по нему считается «сегодня».
    """
    system = SYSTEM_PROMPT.format(
        today=datetime.now(ZoneInfo(timezone) if timezone else USER_TIMEZONE).date().isoformat(),
        categories=", ".join(json.dumps(name, ensure_ascii=False) for name in categories),
    )
    user_content = raw_text
    if source_hint:
        user_content = f"[Источник: {source_hint}]\n{raw_text}"

    response = _client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Groq вернул невалидный JSON: {content!r}") from exc

    tasks = parsed.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError(f"Ожидался список задач в поле 'tasks', получено: {parsed!r}")

    return tasks


MONEY_SYSTEM_PROMPT = """Ты разбираешь сообщение о деньгах: траты и доходы человека.
Сегодня {today} ({weekday}). Валюта по умолчанию: {default_currency}.

Правила:
- Каждая трата или доход — отдельный объект. «продукты 45 евро, проезд 1500 тенге» — два объекта.
- amount — сумма строкой с точкой: «3,5» и «3.5» → "3.50"; «5к», «5 тыс» → "5000".
  Сумму НИКОГДА не выдумывай: если у траты в тексте нет числа, этот объект не возвращай.
- currency: «евро», «€», «eur» → "EUR"; «тенге», «₸», «тг», «kzt» → "KZT";
  если валюта не названа — "{default_currency}". Других валют не бывает.
- kind: "income", если это поступление денег («получил», «зарплата», «пришло», «перевели мне»),
  иначе "expense".
- category — строго одно название из списка пользователя для своего kind:
  расходы (expense): {expense_categories}
  доходы (income): {income_categories}
  Выбирай по смыслу; если ничего не подходит — "Прочее", а если его нет в списке — первое.
- note — короткое описание из текста («Кофе», «Такси»), с большой буквы, без суммы и валюты.
- date — "YYYY-MM-DD": «вчера», «в пятницу», «3 октября» считай от сегодняшней даты;
  прошедший день недели — ближайший в прошлом. Если дата не названа — сегодня ({today}).
  Дату не выдумывай.
- Верни ТОЛЬКО JSON-объект вида {{"items": [...]}}, без пояснений и без markdown.
  Элемент: {{"kind": "expense|income", "amount": "строка", "currency": "EUR|KZT",
  "category": "название", "note": "строка", "date": "YYYY-MM-DD"}}.
  Если денег в тексте нет — {{"items": []}}."""

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def parse_money_text(text: str, categories: dict, today, default_currency: str) -> list[dict]:
    """
    Текст о деньгах → список черновиков {kind, amount, currency, category, note, date}.
    categories — {"expense": [названия], "income": [названия]} пользователя; today — его сегодняшняя дата.
    Ответ модели money_service всё равно сверяет: сумма, валюта, тип, категория и дата.
    Текст ошибки не содержит ответа модели и самого сообщения (они не должны попадать в логи).
    """
    def names(kind):
        return ", ".join(json.dumps(n, ensure_ascii=False) for n in categories.get(kind, [])) or '"Прочее"'

    system = MONEY_SYSTEM_PROMPT.format(
        today=today.isoformat(), weekday=WEEKDAYS_RU[today.weekday()], default_currency=default_currency,
        expense_categories=names("expense"), income_categories=names("income"),
    )
    response = _client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # модель иногда оборачивает JSON в лишний текст — берём первый объект целиком
        start, end = content.find("{"), content.rfind("}")
        try:
            parsed = json.loads(content[start:end + 1]) if start >= 0 and end > start else None
        except json.JSONDecodeError:
            parsed = None
        if parsed is None:
            raise ValueError("модель вернула не JSON")
    items = parsed.get("items", []) if isinstance(parsed, dict) else []
    if not isinstance(items, list):
        raise ValueError("в ответе модели нет списка items")
    return items


MERCHANT_PROMPT = """По названию магазина или сервиса выбери категорию расходов человека.
Категории (только из этого списка): {categories}.
Супермаркеты, кафе, рестораны, доставка еды — еда (в том числе сети Esselunga, Conad, Coop, Carrefour, Lidl,
Eurospin, Pam, Despar, Penny, Aldi, Magnum, Small, Galmart, Arbuz, Wolt, Glovo, Just Eat); такси, транспорт, топливо, парковка —
транспорт (Uber, Bolt, Yandex Go, Trenitalia, Italo, ATM, Eni, Q8);
аптеки и клиники — здоровье; кино, концерты, игры — развлечения; стриминг и подписки — подписки; одежда и обувь — одежда.
Если не уверен — "Прочее" (или первая категория, если "Прочее" нет в списке).
Верни ТОЛЬКО JSON {{"category": "название из списка"}}."""


def classify_merchant(merchant: str, categories: list[str]) -> Optional[str]:
    """Категория расходов по названию магазина (Apple Pay). Модель получает только название и список категорий,
    без суммы. Ответ сверяет money_service; ошибка — None (тогда «Прочее»)."""
    names = ", ".join(json.dumps(n, ensure_ascii=False) for n in categories)
    response = _client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": MERCHANT_PROMPT.format(categories=names)},
                  {"role": "user", "content": merchant[:120]}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    try:
        value = json.loads(response.choices[0].message.content or "{}").get("category")
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """
    Распознаёт голосовое сообщение через Whisper (Groq).
    filename нужен только чтобы Groq понял формат по расширению —
    Telegram присылает голосовые в .ogg (кодек Opus), это поддерживаемый формат.
    """
    transcription = _client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        language="ru",
        response_format="json",
    )
    return transcription.text
