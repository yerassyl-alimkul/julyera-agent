"""
Локальный сервер для проверки интерфейса на НАСТОЯЩЕМ backend (main.py и сервисы) с базой в памяти.

В отличие от design/mock_server.py (подставные ответы), здесь работают настоящие маршруты, проверки
и расчёты, а база — scripts/fake_supabase.py с вымышленными данными: задачи, заметки, операции за полгода
в EUR и KZT, повторяющаяся аренда. Groq и Telegram не вызываются (разбор текста подменён простым правилом).

    venv/bin/python design/dev_server.py      → http://localhost:8002/?key=test123
"""
import os
import random
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import isolation_check as ic  # noqa: E402  setup_local подменяет базу и ставит тестовые переменные
import money_check as mc  # noqa: E402

db = ic.setup_local()
os.environ.pop("TELEGRAM_BOT_TOKEN", None)  # без бота: lifespan не пойдёт в сеть

import uvicorn  # noqa: E402

import categories_service  # noqa: E402
import groq_client  # noqa: E402
import main  # noqa: E402
import money_service  # noqa: E402
import notes_service  # noqa: E402
import task_service  # noqa: E402
import users_service  # noqa: E402

task_service.parse_text_to_tasks = ic.fake_parse
groq_client.parse_money_text = mc.fake_money_parse

owner = users_service.create_user("Yera", 1001)
row = next(u for u in db.tables["users"] if u["id"] == owner["id"])
row.update({"access_key": "test123", "is_admin": True})
uid = owner["id"]
for i, name in enumerate(("Study", "Work", "Personal", "UniMe")):
    categories_service.create_category(uid, name, categories_service.PALETTE[i])
money_service.ensure_defaults(uid)

today = date.today()
for title, cat, days in (("Созвон с командой", "Work", 0), ("Купить продукты", "Personal", 0),
                         ("Сдать отчёт", "Work", 2), ("Прочитать главу", "Study", None),
                         ("Оплатить интернет", "Personal", -2)):
    task_service.supabase.table("tasks").insert({
        "user_id": uid, "task": title, "category": cat, "priority": "Mid",
        "deadline": f"{today + timedelta(days=days)}T18:00:00" if days is not None else None}).execute()
notes_service.supabase.table("notes").insert({"user_id": uid, "content": "Идея: учёт денег в том же приложении"}).execute()

cats = {(c["kind"], c["name"]): c["id"] for c in money_service.list_categories(uid)}
rnd = random.Random(7)
spend = [("Еда", "Кофе", 2, 6), ("Еда", "Продукты", 20, 70), ("Транспорт", "Такси", 8, 25),
         ("Развлечения", "Кино", 10, 20), ("Подписки", "Музыка", 10, 11), ("Здоровье", "Аптека", 5, 30)]
for back in range(0, 170, 3):
    day = today - timedelta(days=back)
    cat, note, lo, hi = rnd.choice(spend)
    money_service.create_transaction(uid, {"kind": "expense", "amount": f"{rnd.uniform(lo, hi):.2f}", "currency": "EUR",
                                           "category_id": cats[("expense", cat)], "note": note, "spent_on": day})
    if back % 15 == 0:
        money_service.create_transaction(uid, {"kind": "expense", "amount": str(rnd.randint(2, 9) * 500), "currency": "KZT",
                                               "category_id": cats[("expense", "Транспорт")], "note": "Проезд", "spent_on": day})
for m in range(6):
    y, mo = divmod(today.month - 1 - m, 12)
    first = date(today.year + y, mo + 1, 1)
    if first <= today:
        money_service.create_transaction(uid, {"kind": "income", "amount": "1800", "currency": "EUR",
                                               "category_id": cats[("income", "Зарплата")], "note": "Зарплата",
                                               "spent_on": first})
for cat_name, limit in (("Транспорт", "30"), ("Подписки", "12"), ("Еда", "200")):
    money_service.update_category(cats[("expense", cat_name)], uid, monthly_limit=limit)
money_service.create_recurring(uid, {"kind": "expense", "amount": "650", "currency": "EUR",
                                     "category_id": cats[("expense", "Жильё")], "day_of_month": 1, "note": "Аренда"})

if __name__ == "__main__":
    print("Интерфейс: http://localhost:8002/?key=test123 (данные вымышленные, в памяти)")
    uvicorn.run(main.app, host="127.0.0.1", port=8002, log_level="warning")
