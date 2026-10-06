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

# «Сегодня» для разбора «завтра», «в пятницу» и приоритета считается по времени владельца,
# а не по часам сервера (UTC): иначе после полуночи по Италии «завтра» сдвигается на день.
# Та же зона, что у событий в google_calendar_service.py.
USER_TIMEZONE = ZoneInfo("Europe/Rome")

SYSTEM_PROMPT = """Ты — ассистент, который превращает произвольный текст (учебный материал,
письмо, список дел) в структурированные задачи.

Правила:
- Извлекай только конкретные, выполнимые действия. Игнорируй общий информационный текст.
- Поле category — строго одно из значений: "Study", "Work", "Personal", "UniMe".
  "UniMe" — конкретные дедлайны/задания по магистратуре в Университете Мессины.
  "Study" — самостоятельное изучение (например, аналитика данных) вне рамок универа.
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
  "category": "Study | Work | Personal | UniMe",
  "source": "строка или null",
  "deadline": "YYYY-MM-DDTHH:MM:SS или null",
  "priority": "High | Mid | Low",
  "note": "строка или пусто",
  "is_recurring": true | false,
  "recurrence_pattern": "daily | weekly | monthly | null",
  "tags": ["строка", ...]
}}"""


def parse_text_to_tasks(raw_text: str, source_hint: Optional[str] = None) -> list[dict]:
    """
    Отправляет сырой текст в Groq и возвращает список словарей,
    готовых к вставке в таблицу tasks (без id/created_at/updated_at).
    """
    system = SYSTEM_PROMPT.format(today=datetime.now(USER_TIMEZONE).date().isoformat())
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
