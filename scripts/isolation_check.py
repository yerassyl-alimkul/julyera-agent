"""
Проверка изоляции данных между пользователями (критерии приёмки задачи multi-user).

Режимы:
    python scripts/isolation_check.py
        Локально: настоящий main.py и сервисы, база в памяти (scripts/fake_supabase.py),
        без сети и без настоящих ключей. Проверяет всё: HTTP, бота, напоминания, Google, секреты.

    python scripts/isolation_check.py --base-url https://julyera-agent.onrender.com
        Против работающего сервиса: только HTTP-проверки изоляции на настоящей базе.
        Нужны SUPABASE_URL, SUPABASE_KEY (service_role) и TELEGRAM_BOT_TOKEN в окружении или .env.
        Создаёт двух временных пользователей и в конце удаляет их вместе с их задачами и заметками.
        Бота, напоминания и Google в этом режиме не трогает: там ушли бы настоящие сообщения.

Ключи, токены и подписи в консоль не выводятся. Код выхода 1, если хоть одна проверка не прошла.
"""
import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name))
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok or not detail else f"  ({detail})"))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------- окружение

def setup_local():
    """Фейковое окружение до импорта приложения: база в памяти, случайные тестовые секреты."""
    from fake_supabase import FakeSupabase

    os.environ.update({
        "SUPABASE_URL": "https://fake.supabase.co",
        # не настоящий ключ: JWT-образная строка, которую принимает конструктор клиента
        "SUPABASE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmb28iOiJiYXIifQ.abc",
        "GROQ_API_KEY": "fake",
        "TELEGRAM_BOT_TOKEN": f"{secrets.randbelow(10**9)}:{secrets.token_urlsafe(26)}",
        "REMINDERS_SECRET": secrets.token_urlsafe(16),
        "OAUTH_STATE_SECRET": secrets.token_urlsafe(32),
        "GOOGLE_CLIENT_ID": "fake-client-id",
        "GOOGLE_CLIENT_SECRET": "fake-client-secret",
        "GOOGLE_REDIRECT_URI": "http://testserver/google/callback",
    })
    os.environ.pop("WEBHOOK_BASE_URL", None)
    db = FakeSupabase()
    database = types.ModuleType("database")
    database.supabase = db
    sys.modules["database"] = database
    import rates_service  # без сети: постоянный курс для проверок (1 € = 500,35 ₸)
    from decimal import Decimal
    rates_service.get_rates = lambda: {"date": "2026-10-08", "kzt_per": {"KZT": Decimal(1), "EUR": Decimal("500.35")}}
    return db


PARSE_CALLS = []


def fake_parse(raw_text, categories, source_hint=None, timezone=None):
    """Вместо Groq: каждая строка текста — задача без даты. Запоминает, какие категории ей дали.
    Метки в тексте имитируют ошибку модели: «#чужая» — категория другого пользователя, «#выдумка» — несуществующая."""
    PARSE_CALLS.append(list(categories))
    out = []
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        category = "Учёба A" if "#чужая" in line else "Выдуманная" if "#выдумка" in line else categories[0]
        out.append({"task": line.strip(), "category": category, "priority": "Mid", "tags": []})
    return out


def give_categories(user, *names):
    import categories_service
    palette = categories_service.PALETTE
    for i, name in enumerate(names):
        categories_service.create_category(user["id"], name, palette[i % len(palette)])


# ---------------------------------------------------------------- заголовки входа

def init_data_headers(tg_id, token, name="Тест", auth_date=None, tamper=False):
    from telegram_auth import sign_init_data

    fields = {
        "query_id": "AAH" + secrets.token_hex(6),
        "user": json.dumps({"id": tg_id, "first_name": name}, ensure_ascii=False, separators=(",", ":")),
        "auth_date": int(time.time() if auth_date is None else auth_date),
    }
    signed = sign_init_data(fields, token)
    if tamper:  # подменяем Telegram-id после подписи
        signed = signed.replace(f"%22id%22%3A{tg_id}", f"%22id%22%3A{tg_id + 1}")
    return {"X-Telegram-Init-Data": signed}


def key_headers(user):
    return {"X-Access-Key": user["access_key"]}


# ---------------------------------------------------------------- HTTP-проверки (оба режима)

def http_checks(client, token, A, B, owner, unknown_tg_id, count_users_with_tg):
    hA = init_data_headers(A["telegram_user_id"], token, "Тест A")
    hB = init_data_headers(B["telegram_user_id"], token, "Тест B")

    section("Вход")
    for path in ["/tasks", "/subtasks", "/notes", "/google/status"]:
        check(f"без заголовков {path} → 401", client.get(path).status_code == 401)
    check("/health открывается без заголовков", client.get("/health").status_code == 200)
    root = client.get("/")
    check("главная страница открывается без заголовков", root.status_code == 200 and "<html" in root.text.lower())

    tg = A["telegram_user_id"]
    check("initData с изменённым полем → 401",
          client.get("/tasks", headers=init_data_headers(tg, token, tamper=True)).status_code == 401)
    other_token = f"{secrets.randbelow(10**9)}:{secrets.token_urlsafe(26)}"
    check("initData с подписью другого токена → 401",
          client.get("/tasks", headers=init_data_headers(tg, other_token)).status_code == 401)
    check("initData старше 24 часов → 401",
          client.get("/tasks", headers=init_data_headers(tg, token, auth_date=time.time() - 25 * 3600)).status_code == 401)
    check("неверный X-Access-Key → 401",
          client.get("/tasks", headers={"X-Access-Key": secrets.token_hex(32)}).status_code == 401)

    before = count_users_with_tg(unknown_tg_id)
    r = client.get("/tasks", headers=init_data_headers(unknown_tg_id, token, "Незнакомец"))
    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
    check("верный initData незнакомого человека → 403 not_approved",
          r.status_code == 403 and isinstance(detail, dict) and detail.get("code") == "not_approved")
    check("…и в базе ничего не создано", count_users_with_tg(unknown_tg_id) == before == 0)

    section("Задачи")
    ta = client.post("/tasks", headers=hA, json={"task": "Задача A (изоляция)", "category": "Work"}).json()
    tasks_b = client.get("/tasks", headers=hB).json()
    check("задача A не видна в GET /tasks у B", all(t["id"] != ta["id"] for t in tasks_b))
    check("задача A видна самому A", any(t["id"] == ta["id"] for t in client.get("/tasks", headers=hA).json()))
    check("GET /tasks/{чужая} → 404", client.get(f"/tasks/{ta['id']}", headers=hB).status_code == 404)
    check("PATCH /tasks/{чужая} → 404",
          client.patch(f"/tasks/{ta['id']}", headers=hB, json={"task": "взлом", "status": "Done"}).status_code == 404)
    check("DELETE /tasks/{чужая} → 404", client.delete(f"/tasks/{ta['id']}", headers=hB).status_code == 404)
    after = client.get(f"/tasks/{ta['id']}", headers=hA)
    check("…и задача A не изменилась", after.status_code == 200
          and after.json()["task"] == "Задача A (изоляция)" and after.json()["status"] == "To Do")

    section("Подзадачи")
    sa = client.post(f"/tasks/{ta['id']}/subtasks", headers=hA, json={"content": "Шаг A"}).json()
    check("POST /tasks/{чужая}/subtasks → 404",
          client.post(f"/tasks/{ta['id']}/subtasks", headers=hB, json={"content": "чужой шаг"}).status_code == 404)
    check("GET /subtasks у B без подзадач A",
          all(s["id"] != sa["id"] for s in client.get("/subtasks", headers=hB).json()))
    check("PATCH /subtasks/{чужая} → 404",
          client.patch(f"/subtasks/{sa['id']}", headers=hB, json={"is_done": True}).status_code == 404)
    check("DELETE /subtasks/{чужая} → 404", client.delete(f"/subtasks/{sa['id']}", headers=hB).status_code == 404)
    subs_a = [s for s in client.get("/subtasks", headers=hA).json() if s["id"] == sa["id"]]
    check("…и подзадача A на месте и не отмечена", len(subs_a) == 1 and subs_a[0]["is_done"] is False)

    section("Заметки")
    na = client.post("/notes", headers=hA, json={"content": "Заметка A"}).json()
    check("заметка A не видна в GET /notes у B", all(n["id"] != na["id"] for n in client.get("/notes", headers=hB).json()))
    check("PATCH /notes/{чужая} → 404",
          client.patch(f"/notes/{na['id']}", headers=hB, json={"content": "взлом"}).status_code == 404)
    check("DELETE /notes/{чужая} → 404", client.delete(f"/notes/{na['id']}", headers=hB).status_code == 404)
    notes_a = [n for n in client.get("/notes", headers=hA).json() if n["id"] == na["id"]]
    check("…и заметка A не изменилась", len(notes_a) == 1 and notes_a[0]["content"] == "Заметка A")

    section("user_id от клиента игнорируется")
    tb = client.post("/tasks", headers=hB, json={"task": "Задача B с чужим user_id", "category": "Work", "user_id": A["id"]})
    tb_id = tb.json().get("id")
    check("POST /tasks с user_id=A от B создаёт задачу у B",
          tb.status_code == 201 and any(t["id"] == tb_id for t in client.get("/tasks", headers=hB).json())
          and all(t["id"] != tb_id for t in client.get("/tasks", headers=hA).json()))
    client.patch(f"/tasks/{tb_id}", headers=hB, json={"user_id": A["id"], "task": "Задача B (после PATCH)"})
    check("PATCH с user_id=A не передаёт задачу A",
          all(t["id"] != tb_id for t in client.get("/tasks", headers=hA).json())
          and any(t["id"] == tb_id for t in client.get("/tasks", headers=hB).json()))
    nb = client.post("/notes", headers=hB, json={"content": "Заметка B", "user_id": A["id"]}).json()
    check("POST /notes с user_id=A от B создаёт заметку у B",
          all(n["id"] != nb["id"] for n in client.get("/notes", headers=hA).json()))

    section("Вход по ключу из /web")
    via_key = client.get("/tasks", headers=key_headers(A))
    ids = {t["id"] for t in via_key.json()} if via_key.status_code == 200 else set()
    check("X-Access-Key A открывает только задачи A", via_key.status_code == 200 and ta["id"] in ids and tb_id not in ids)

    section("Категории")
    from categories_service import PALETTE
    ca = client.post("/categories", headers=hA, json={"name": "Учёба A", "color": PALETTE[5]})
    ca_id = ca.json().get("id")
    check("A создаёт категорию", ca.status_code == 201 and ca.json().get("name") == "Учёба A")
    check("категория A не видна в GET /categories у B",
          all(c["id"] != ca_id for c in client.get("/categories", headers=hB).json()))
    check("PATCH /categories/{чужая} → 404",
          client.patch(f"/categories/{ca_id}", headers=hB, json={"name": "взлом"}).status_code == 404)
    check("DELETE /categories/{чужая} → 404", client.delete(f"/categories/{ca_id}", headers=hB).status_code == 404)
    ids_b = [c["id"] for c in client.get("/categories", headers=hB).json()]
    check("PUT /categories/order с чужим id отклоняется",
          client.put("/categories/order", headers=hB, json={"ids": ids_b + [ca_id]}).status_code == 422)
    check("цвет вне палитры → 422",
          client.post("/categories", headers=hA, json={"name": "Цвет", "color": "#123456"}).status_code == 422)
    check("дубль названия без учёта регистра → 409",
          client.post("/categories", headers=hA, json={"name": "  учёба a ", "color": PALETTE[0]}).status_code == 409)
    check("название длиннее 30 символов → 422",
          client.post("/categories", headers=hA, json={"name": "x" * 31, "color": PALETTE[0]}).status_code == 422)
    check("задача B с категорией A → 422",
          client.post("/tasks", headers=hB, json={"task": "чужая категория", "category": "Учёба A"}).status_code == 422)
    t_cat = client.post("/tasks", headers=hA, json={"task": "Задача в Учёба A", "category": "учёба a"}).json()
    check("категория задачи сохраняется в написании пользователя", t_cat.get("category") == "Учёба A")
    client.patch(f"/categories/{ca_id}", headers=hA, json={"name": "Универ A", "color": PALETTE[6]})
    renamed = client.get(f"/tasks/{t_cat['id']}", headers=hA).json()
    cats_a = {c["id"]: c for c in client.get("/categories", headers=hA).json()}
    check("переименование меняет категорию у существующих задач",
          renamed.get("category") == "Универ A" and cats_a[ca_id]["color"] == PALETTE[6])
    check("GET /tasks?category= фильтрует по названию",
          [t["id"] for t in client.get("/tasks", headers=hA, params={"category": "Универ A"}).json()] == [t_cat["id"]])
    check("удаление категории с задачами без move_to → 409",
          client.delete(f"/categories/{ca_id}", headers=hA).status_code == 409)
    work_a = next(c for c in cats_a.values() if c["name"] == "Work")
    r = client.delete(f"/categories/{ca_id}", headers=hA, params={"move_to": work_a["id"]})
    moved = client.get(f"/tasks/{t_cat['id']}", headers=hA).json()
    check("удаление с move_to переносит задачи", r.status_code == 204 and moved.get("category") == "Work")
    order = [c["id"] for c in client.get("/categories", headers=hA).json()][::-1]
    client.put("/categories/order", headers=hA, json={"ids": order})
    check("порядок категорий меняется", [c["id"] for c in client.get("/categories", headers=hA).json()] == order)
    only = client.post("/categories", headers=hB, json={"name": "Временная B", "color": PALETTE[7]}).json()
    for c in client.get("/categories", headers=hB).json():
        if c["id"] != only["id"]:
            client.delete(f"/categories/{c['id']}", headers=hB, params={"move_to": only["id"]})
    left = client.get("/categories", headers=hB).json()
    check("последнюю категорию удалить нельзя (409)",
          len(left) == 1 and client.delete(f"/categories/{left[0]['id']}", headers=hB).status_code == 409)
    check("у A после этого ничего не изменилось",
          {c["name"] for c in client.get("/categories", headers=hA).json()} == {"Work", "Personal"})
    client.post("/categories", headers=hB, json={"name": "Work", "color": PALETTE[1]})  # вернуть B рабочий набор

    section("Повторяющиеся задачи")
    rec = client.post("/tasks", headers=hA, json={
        "task": "Повтор A", "category": "Personal", "deadline": "2026-10-10T09:00:00",
        "is_recurring": True, "recurrence_pattern": "weekly"}).json()
    client.patch(f"/tasks/{rec['id']}", headers=hA, json={"status": "Done"})
    next_a = [t for t in client.get("/tasks", headers=hA).json() if t["task"] == "Повтор A" and t["status"] == "To Do"]
    check("закрытие повторяющейся задачи создаёт следующую у того же пользователя",
          len(next_a) == 1 and next_a[0]["deadline"].startswith("2026-10-17"))
    check("…и у B её нет", all(t["task"] != "Повтор A" for t in client.get("/tasks", headers=hB).json()))

    return {"task_a": ta, "task_b_id": tb_id}


# ---------------------------------------------------------------- статические проверки

def static_checks():
    section("Код и репозиторий")
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    bad = re.findall(r"new Date\([^)]*deadline[^)]*\)", html)
    check("в index.html нет new Date(…deadline…), время через parseNaiveDeadline",
          not bad and "function parseNaiveDeadline" in html)
    patterns = [
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT (service_role и т.п.)
        r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}",                                 # токен Telegram-бота
        r"gsk_[A-Za-z0-9]{20,}",                                              # ключ Groq
        r"GOCSPX-[A-Za-z0-9_-]{10,}",                                         # секрет Google OAuth
        r"sb_secret_[A-Za-z0-9_-]{10,}",                                      # секретный ключ Supabase
    ]
    out = subprocess.run(["git", "grep", "-nIE", "|".join(patterns)], cwd=ROOT, capture_output=True, text=True)
    # Единственное разрешённое совпадение — заведомо фейковый JWT {"foo":"bar"} для тестов.
    hits = [line.split(":", 2)[:2] for line in out.stdout.splitlines()
            if "eyJmb28iOiJiYXIifQ" not in line]
    check("в репозитории нет токенов и ключей (git grep)", not hits,
          "найдено в: " + ", ".join(":".join(h) for h in hits))  # печатаем только файл:строку


# ---------------------------------------------------------------- локальный режим: бот, напоминания, Google

class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, markup=reply_markup))

    async def get_chat(self, chat_id):
        return SimpleNamespace(first_name="Жена", last_name=None)


def tg_update(tg_id, text=None, callback_data=None, first_name="Тест"):
    replies, answers, edits = [], [], []

    async def reply_text(t, **kw):
        replies.append(SimpleNamespace(text=t, markup=kw.get("reply_markup")))

    async def send_action(_):
        return None

    async def answer(t=None, show_alert=False):
        answers.append(t)

    async def edit_message_text(t, **_):
        edits.append(t)

    user = SimpleNamespace(id=tg_id, first_name=first_name, last_name=None, username=None)
    query = None
    if callback_data:
        query = SimpleNamespace(from_user=user, data=callback_data, answer=answer, edit_message_text=edit_message_text,
                                message=SimpleNamespace(text="Запрос доступа: Жена, Telegram-id 3003."))
    message = SimpleNamespace(text=text, reply_text=reply_text, chat=SimpleNamespace(send_action=send_action), voice=None)
    return SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=tg_id), message=message,
                           callback_query=query, replies=replies, answers=answers, edits=edits)


def telegram_checks(db, client, owner, A):
    import telegram_handlers as h
    import users_service

    run = asyncio.run
    section("Telegram")
    ctx = SimpleNamespace(bot=FakeBot())
    stranger = 3003
    tasks_before = len(db.tables.get("tasks", []))
    u = tg_update(stranger, text="Купить слона")
    run(h.handle_text(u, ctx))
    check("сообщение из неизвестного чата ничего не создаёт",
          len(db.tables.get("tasks", [])) == tasks_before and "Нет доступа" in u.replies[-1].text)

    u = tg_update(stranger, text="/start", first_name="Жена")
    run(h.start(u, ctx))
    to_owner = [m for m in ctx.bot.sent if m.chat_id == owner["telegram_user_id"]]
    check("/start от незнакомца шлёт владельцу запрос с кнопками",
          len(to_owner) == 1 and "Разрешить" in str(to_owner[0].markup) and "Запрос отправлен" in u.replies[-1].text)
    run(h.start(tg_update(stranger, text="/start"), ctx))
    check("повторный /start в течение часа не шлёт второй запрос",
          len([m for m in ctx.bot.sent if m.chat_id == owner["telegram_user_id"]]) == 1)

    u = tg_update(A["telegram_user_id"], callback_data=f"approve:{stranger}")
    run(h.access_decision(u, ctx))
    check("«Разрешить» не от админа отклоняется", users_service.get_by_telegram_id(stranger) is None and u.answers)

    u = tg_update(owner["telegram_user_id"], callback_data=f"approve:{stranger}")
    run(h.access_decision(u, ctx))
    wife = users_service.get_by_telegram_id(stranger)
    got_button = [m for m in ctx.bot.sent if m.chat_id == stranger and "web_app" in str(m.markup)]
    check("«Разрешить» от админа создаёт пользователя и шлёт ему кнопку приложения",
          wife is not None and wife["name"] == "Жена" and got_button and "Разрешено" in u.edits[-1])
    run(h.access_decision(tg_update(owner["telegram_user_id"], callback_data=f"approve:{stranger}"), ctx))
    check("повторное «Разрешить» ничего не дублирует",
          len([x for x in db.tables["users"] if x.get("telegram_user_id") == stranger]) == 1)
    wife_cats = [c["name"] for c in db.tables["categories"] if c["user_id"] == wife["id"]]
    check("при одобрении создаётся одна категория «Общее»", wife_cats == ["Общее"])

    u = tg_update(stranger, text="Записаться к врачу")
    run(h.handle_text(u, ctx))
    wife_tasks = [t for t in db.tables["tasks"] if t["task"] == "Записаться к врачу"]
    check("задача из её чата создаётся у неё",
          len(wife_tasks) == 1 and wife_tasks[0]["user_id"] == wife["id"])
    check("парсер получает только её категории", PARSE_CALLS[-1] == ["Общее"])
    run(h.handle_text(tg_update(stranger, text="Чужая категория #чужая\nВыдуманная #выдумка"), ctx))
    odd = [t for t in db.tables["tasks"] if t["user_id"] == wife["id"] and "#" in t["task"]]
    check("несуществующая или чужая категория от модели → запасная («Общее»)",
          len(odd) == 2 and all(t["category"] == "Общее" for t in odd))
    u = tg_update(stranger, text="/categories")
    run(h.categories_handler(u, ctx))
    check("/categories показывает только её категории",
          "Общее" in u.replies[-1].text and "Study" not in u.replies[-1].text)
    hA = key_headers(A)
    check("…и не видна другим",
          all(t["task"] != "Записаться к врачу" for t in client.get("/tasks", headers=hA).json()))
    u = tg_update(stranger, text="/list")
    run(h.list_tasks_handler(u, ctx))
    listing = u.replies[-1].text
    check("/list показывает только её задачи", "Записаться к врачу" in listing and "Задача A" not in listing)

    u = tg_update(stranger, text="/web")
    run(h.web_link(u, ctx))
    m = re.search(r"\?key=([0-9a-f]+)", u.replies[-1].text)
    via_link = client.get("/tasks", headers={"X-Access-Key": m.group(1)}) if m else None
    check("/web присылает ссылку, которая открывает только её данные",
          via_link is not None and via_link.status_code == 200
          and {t["id"] for t in via_link.json()} == {t["id"] for t in db.tables["tasks"] if t["user_id"] == wife["id"]})
    return wife


def reminder_checks(client, A, B, wife):
    from datetime import datetime as dt, timezone as tz
    from zoneinfo import ZoneInfo

    import digest_service
    import main
    import users_service

    section("Напоминания")
    # 13:15 по Риму — окно напоминания о дедлайнах (digest_service.SLOTS)
    rome_now = dt.now(ZoneInfo("Europe/Rome"))
    at = rome_now.replace(hour=13, minute=15, second=0, microsecond=0)
    digest_service.utc_now = lambda: at.astimezone(tz.utc)
    today = at.strftime("%Y-%m-%d")
    hA, hB = key_headers(A), key_headers(B)
    client.post("/tasks", headers=hA, json={"task": "Напомнить A", "category": "Work", "deadline": f"{today}T18:00:00"})
    client.post("/tasks", headers=hB, json={"task": "Напомнить B", "category": "Work", "deadline": f"{today}T19:00:00"})
    client.post("/tasks", headers=hA, json={"task": "Далёкая A", "category": "Work", "deadline": "2030-01-01T09:00:00"})
    users_service.link_chat(A["telegram_user_id"], A["id"])
    users_service.link_chat(B["telegram_user_id"], B["id"])

    sent = []

    async def fake_send(_client, chat_id, text, buttons=None):
        sent.append((chat_id, text))
        return True

    real = main._send_telegram_message
    main._send_telegram_message = fake_send
    try:
        r = client.post("/internal/reminders/run", headers={"X-Reminder-Secret": os.environ["REMINDERS_SECRET"]})
    finally:
        main._send_telegram_message = real
    to_a = "\n".join(t for c, t in sent if c == A["telegram_user_id"])
    to_b = "\n".join(t for c, t in sent if c == B["telegram_user_id"])
    check("напоминания: каждому только его задачи со сроком в ближайшие 2 дня",
          r.status_code == 200 and "Напомнить A" in to_a and "Напомнить B" not in to_a
          and "Напомнить B" in to_b and "Напомнить A" not in to_b and "Далёкая A" not in to_a)
    check("напоминание без секрета → 403", client.post("/internal/reminders/run").status_code == 403)


def google_checks(db, client, owner, A, B):
    import httplib2
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    import google_calendar_service as gcal
    import notifier

    section("Google Calendar")
    state = gcal.make_state(A["id"])
    check("state подписан и раскрывается в user_id", gcal.verify_state(state) == A["id"])
    forged = state.replace(A["id"], B["id"], 1)
    check("подделанный state отклоняется", gcal.verify_state(forged) is None)
    check("state старше 10 минут отклоняется", gcal.verify_state(state, now=time.time() + 601) is None)
    r = client.get("/google/callback", params={"code": "x", "state": forged})
    check("callback с подделанным state не сохраняет токен",
          r.status_code == 400 and not db.tables.get("google_credentials"))
    url = client.get("/google/auth-url", headers=key_headers(A)).json().get("url", "")
    check("/google/auth-url отдаёт адрес Google со state текущего пользователя",
          url.startswith("https://accounts.google.com/") and "state=" in url)
    check("/google/auth-url без входа → 401", client.get("/google/auth-url").status_code == 401)

    # Сеть Google и Telegram подменены: запоминаем, чьим токеном пишется событие, что в нём и кому ушли сообщения.
    future = "2030-01-01T00:00:00+00:00"
    db.tables["google_credentials"] = [
        {"user_id": A["id"], "access_token": "token-A", "refresh_token": "r-A", "token_expiry": future, "needs_reconnect": False},
        {"user_id": B["id"], "access_token": "token-B", "refresh_token": "r-B", "token_expiry": future, "needs_reconnect": False},
    ]
    used, bodies, sent, broken_api, dead_refresh = [], [], [], set(), set()

    class Exec:
        def __init__(self, token, body=None):
            self.token, self.body = token, body

        def execute(self):
            if self.token in broken_api:  # Google отвечает 401: доступ отозван
                raise HttpError(httplib2.Response({"status": 401}), b'{"error": "unauthorized"}')
            used.append(self.token)
            if self.body:
                bodies.append(self.body)
            return {"id": f"evt-{self.token}-{len(used)}"}

    def fake_build(*_, credentials=None, **__):
        token = credentials.token
        events = SimpleNamespace(insert=lambda **kw: Exec(token, kw.get("body")),
                                 update=lambda **kw: Exec(token, kw.get("body")),
                                 delete=lambda **kw: Exec(token))
        return SimpleNamespace(events=lambda: events)

    def fake_refresh(creds):
        if creds.refresh_token in dead_refresh:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    class FakeFlow:
        @staticmethod
        def from_client_config(*_, **__):
            flow = SimpleNamespace(credentials=None)
            flow.fetch_token = lambda code: setattr(flow, "credentials", SimpleNamespace(
                token="token-A2", refresh_token="r-A2", expiry=None))
            return flow

    saved = (gcal.build, gcal._refresh, gcal.Flow, notifier.send_telegram)
    gcal.build, gcal._refresh, gcal.Flow = fake_build, fake_refresh, FakeFlow
    notifier.send_telegram = lambda chat_id, text, *_, **__: sent.append((chat_id, text)) or True
    gcal._status_cache.clear()
    task = lambda h, name, deadline: client.post("/tasks", headers=h, json={"task": name, "category": "Work", "deadline": deadline})
    try:
        hA, hB = key_headers(A), key_headers(B)
        task(hA, "Событие A", "2030-01-02T09:00:00")
        task(hB, "Событие B", "2030-01-02T10:00:00")
        check("события пишутся токеном своего пользователя", used == ["token-A", "token-B"])

        st = lambda h: client.get("/google/status", headers=h).json()
        check("статус: не подключён (нет токена)", st(key_headers(owner)) == {"configured": True, "connected": False, "needs_reconnect": False})
        check("статус: подключён (токен проверен вживую)", st(hA) == {"configured": True, "connected": True, "needs_reconnect": False})

        # Мёртвый refresh-токен у B: живая проверка → «нужно переподключить», ровно одно сообщение в её чат
        dead_refresh.add("r-B")
        gcal._status_cache.clear()
        first = st(hB)
        second = st(hB)
        to_b = [t for c, t in sent if c == B["telegram_user_id"]]
        check("испорченный refresh-токен → «Нужно переподключить»", first["needs_reconnect"] is True and second["needs_reconnect"] is True)
        check("…и ровно одно сообщение бота", len(to_b) == 1 and "переподключите" in to_b[0])

        # 401 от API у A при синхронизации → пометка, сообщение, задачи всё равно создаются
        broken_api.add("token-A")
        r1 = task(hA, "При отозванном доступе", "2030-01-03T09:00:00")
        calls_before = len(used)
        r2 = task(hA, "Ещё одна при мёртвом токене", "2030-01-03T10:00:00")
        to_a = [t for c, t in sent if c == A["telegram_user_id"]]
        check("задачи создаются и при мёртвом токене", r1.status_code == 201 and r2.status_code == 201)
        check("401 от Google ставит «Нужно переподключить» и шлёт одно сообщение",
              st(hA)["needs_reconnect"] is True and len(to_a) == 1)
        check("пока токен мёртв, синхронизация пропускается", len(used) == calls_before)
        rb = task(hB, "У B своё", "2030-01-03T11:00:00")
        check("ошибка календаря у одного не ломает задачи другого", rb.status_code == 201)

        # Переподключение A через callback с подписанным state снимает пометку
        broken_api.discard("token-A")
        r = client.get("/google/callback", params={"code": "new", "state": gcal.make_state(A["id"])})
        check("после переподключения снова «Подключён»",
              r.status_code == 200 and "Готово" in r.text and st(hA) == {"configured": True, "connected": True, "needs_reconnect": False})

        # Пояс пользователя: «18:00» остаётся 18:00 в его поясе
        next(u for u in db.tables["users"] if u["id"] == A["id"])["timezone"] = "Asia/Almaty"
        task(hA, "Встреча в 18:00", "2030-01-05T18:00:00")
        body = bodies[-1]
        check("событие на 18:00 в поясе пользователя",
              body["start"] == {"dateTime": "2030-01-05T18:00:00", "timeZone": "Asia/Almaty"} and used[-1] == "token-A2")
    finally:
        gcal.build, gcal._refresh, gcal.Flow, notifier.send_telegram = saved


# ---------------------------------------------------------------- запуск

def run_local():
    db = setup_local()
    from fastapi.testclient import TestClient

    import main
    import task_service
    import users_service

    task_service.parse_text_to_tasks = fake_parse
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    # Владелец с уже существующей задачей (как после миграции) и два тестовых пользователя.
    owner = users_service.create_user("Yera", 1001)
    db.tables["users"][0]["is_admin"] = True
    owner["is_admin"] = True
    db.tables.setdefault("tasks", []).append({**__import__("fake_supabase").DEFAULTS["tasks"](),
                                              "user_id": owner["id"], "task": "Старая задача Yera",
                                              "category": "UniMe", "deadline": "2026-10-01T09:00:00+00:00"})
    A = users_service.create_user("Тест A", 2001)
    B = users_service.create_user("Тест B", 2002)
    give_categories(owner, "Study", "Work", "Personal", "UniMe")
    give_categories(A, "Work", "Personal")
    give_categories(B, "Work", "Personal")
    # без with: lifespan (вебхук Telegram) не запускается; ошибки сервера — ответ 500, а не падение скрипта
    client = TestClient(main.app, raise_server_exceptions=False)

    def count_users_with_tg(tg_id):
        return len([u for u in db.tables["users"] if u.get("telegram_user_id") == tg_id])

    print("Локальная проверка (база в памяти, без сети)")
    http_checks(client, token, A, B, owner, 999001, count_users_with_tg)
    section("Существующие данные")
    owner_tasks = client.get("/tasks", headers=key_headers(owner)).json()
    check("задачи Yera на месте и видны только ему", [t["task"] for t in owner_tasks] == ["Старая задача Yera"])
    wife = telegram_checks(db, client, owner, A)
    reminder_checks(client, A, B, wife)
    google_checks(db, client, owner, A, B)
    static_checks()


def run_remote(base_url):
    import httpx
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
    for name in ("SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_BOT_TOKEN"):
        if not os.environ.get(name):
            sys.exit(f"Нужна переменная {name} (значение не печатается)")
    from database import supabase
    import users_service

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    base_tg = 9_000_000_000 + secrets.randbelow(10**6) * 10
    created = []
    print(f"Проверка против {base_url} (временные пользователи будут удалены)")
    try:
        A = users_service.create_user("isolation-check A", base_tg + 1)
        created.append(A)
        B = users_service.create_user("isolation-check B", base_tg + 2)
        created.append(B)
        give_categories(A, "Work", "Personal")
        give_categories(B, "Work", "Personal")
        owner = next((u for u in users_service.list_admins()), None)

        def count_users_with_tg(tg_id):
            return len(supabase.table("users").select("id").eq("telegram_user_id", tg_id).execute().data)

        with httpx.Client(base_url=base_url, timeout=90) as client:
            client.get("/health")  # разбудить сервер на Render
            http_checks(client, token, A, B, owner, base_tg + 3, count_users_with_tg)
        section("Существующие данные")
        n = len(supabase.table("tasks").select("id").eq("user_id", owner["id"]).execute().data) if owner else 0
        check(f"у владельца есть задачи ({n})", n > 0)
        check("у владельца заполнен telegram_user_id", bool(owner and owner.get("telegram_user_id")))
    finally:
        for user in created:
            supabase.table("tasks").delete().eq("user_id", user["id"]).execute()  # подзадачи удалятся каскадом
            supabase.table("notes").delete().eq("user_id", user["id"]).execute()
            supabase.table("telegram_chats").delete().eq("user_id", user["id"]).execute()
            supabase.table("google_credentials").delete().eq("user_id", user["id"]).execute()
            supabase.table("categories").delete().eq("user_id", user["id"]).execute()
            supabase.table("users").delete().eq("id", user["id"]).execute()
        print(f"\nВременные пользователи удалены: {len(created)}")
    static_checks()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="адрес работающего сервиса; без него — локальная проверка")
    args = parser.parse_args()
    if args.base_url:
        run_remote(args.base_url.rstrip("/"))
    else:
        run_local()
    failed = [name for ok, name in results if not ok]
    print(f"\nИтого: {len(results) - len(failed)} из {len(results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
