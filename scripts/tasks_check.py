"""
Проверки задач: точные повторы (по будням, по дням недели, каждый месяц того же числа, каждый год),
«Разбей на шаги», задачи из текста без сохранения (заметки) и /split в боте.

    python scripts/tasks_check.py

Только локально: настоящий main.py и сервисы, база в памяти, Groq подменён. Код выхода 1, если хоть одна
проверка не прошла.
"""
import asyncio
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import groq_client
    import main as app_main
    import task_service
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    A = users_service.create_user("Задачи A", 4001)
    B = users_service.create_user("Задачи B", 4002)
    for u in (A, B):
        ic.give_categories(u, "Work", "Учёба")
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hA, hB = ic.key_headers(A), ic.key_headers(B)
    nx = task_service.next_occurrence
    D = lambda s: datetime.fromisoformat(s)

    section("Следующий срок повторяющейся задачи")
    today = date(2026, 10, 9)  # пятница
    check("каждый день", nx(D("2026-10-09T09:00"), "daily", None, today) == D("2026-10-10T09:00"))
    check("по будням: после пятницы — понедельник", nx(D("2026-10-09T09:00"), "weekdays", None, today) == D("2026-10-12T09:00"))
    check("по дням недели (пн, ср): после пн — ср", nx(D("2026-10-12T18:00"), "days", [0, 2], today) == D("2026-10-14T18:00"))
    check("…после ср — следующий пн", nx(D("2026-10-14T18:00"), "days", [0, 2], today) == D("2026-10-19T18:00"))
    check("каждый месяц того же числа, а не +30 дней", nx(D("2026-10-15T10:00"), "monthly", [15], today) == D("2026-11-15T10:00"))
    check("31-е → последний день короткого месяца", nx(D("2027-01-31T10:00"), "monthly", [31], date(2027, 1, 31)) == D("2027-02-28T10:00"))
    check("…и обратно на 31-е в длинном", nx(D("2027-02-28T10:00"), "monthly", [31], date(2027, 2, 28)) == D("2027-03-31T10:00"))
    check("каждый год (29 февраля → 28-е)", nx(D("2028-02-29T09:00"), "yearly", [29], date(2028, 2, 29)) == D("2029-02-28T09:00"))
    check("закрыли с опозданием — прошедшие повторы пропускаются",
          nx(D("2026-10-01T09:00"), "daily", None, today) == D("2026-10-10T09:00"))
    check("старый «раз в неделю» работает как раньше", nx(D("2026-10-12T09:00"), "weekly", None, today) == D("2026-10-19T09:00"))

    section("Повтор в приложении")
    r = client.post("/tasks", headers=hA, json={"task": "Спортзал", "category": "Work", "deadline": "2030-01-07T09:00:00",
                                                "recurrence_pattern": "days", "recurrence_days": [2, 0, 0, 9]})
    t = r.json()
    check("«по дням недели» сохраняется: дни по порядку, без повторов и чужих чисел",
          r.status_code == 201 and t["is_recurring"] is True and t["recurrence_days"] == [0, 2], r.text[:200])
    r = client.post("/tasks", headers=hA, json={"task": "Без дней", "category": "Work", "deadline": "2030-01-07T09:00:00",
                                                "recurrence_pattern": "days", "recurrence_days": []})
    check("«по дням недели» без дней → 422", r.status_code == 422)
    r = client.post("/tasks", headers=hA, json={"task": "Аренда", "category": "Work", "deadline": "2030-01-31T10:00:00",
                                                "recurrence_pattern": "monthly"})
    rent = r.json()
    check("«каждый месяц»: число берётся из даты", rent["recurrence_days"] == [31])
    r = client.patch(f"/tasks/{rent['id']}", headers=hA, json={"status": "Done"})
    nxt = [x for x in db.tables["tasks"] if x["task"] == "Аренда" and x["status"] != "Done"]
    check("закрыли — следующая 28 февраля, с тем же правилом",
          len(nxt) == 1 and nxt[0]["deadline"].startswith("2030-02-28T10:00") and nxt[0]["recurrence_days"] == [31], str(nxt))
    r = client.patch(f"/tasks/{t['id']}", headers=hA, json={"recurrence_pattern": None})
    check("повтор можно снять", r.json()["is_recurring"] is False and r.json()["recurrence_days"] is None)
    r = client.patch(f"/tasks/{t['id']}", headers=hA, json={"recurrence_pattern": "weekdays", "recurrence_days": [5]})
    check("«по будням»: лишние дни не сохраняются", r.json()["recurrence_pattern"] == "weekdays" and r.json()["recurrence_days"] is None)

    section("Разбей на шаги")
    seen = []

    def fake_steps(task, note, existing):
        seen.append((task, list(existing)))
        return ["Собрать конспекты", "Повторить темы 1–5", "собрать конспекты", "  ", "Решить билет"]
    groq_client.suggest_steps = fake_steps
    exam = client.post("/tasks", headers=hA, json={"task": "Подготовиться к экзамену", "category": "Учёба"}).json()
    client.post(f"/tasks/{exam['id']}/subtasks", headers=hA, json={"content": "Решить билет"})
    r = client.post(f"/tasks/{exam['id']}/breakdown", headers=hA)
    added = [s["content"] for s in r.json().get("added", [])]
    check("шаги добавлены сразу, без повторов и пустых", r.status_code == 201
          and added == ["Собрать конспекты", "Повторить темы 1–5"], str(added))
    check("модель видит уже существующие шаги", seen[-1] == ("Подготовиться к экзамену", ["Решить билет"]))
    check("чужая задача → 404", client.post(f"/tasks/{exam['id']}/breakdown", headers=hB).status_code == 404)
    groq_client.suggest_steps = lambda *a: (_ for _ in ()).throw(RuntimeError("groq"))
    check("ИИ не ответил → 502 с понятным текстом",
          client.post(f"/tasks/{exam['id']}/breakdown", headers=hA).status_code == 502)

    section("Задачи из заметки (без сохранения)")

    def fake_parse(raw_text, categories, source_hint=None, timezone=None, default_date=None):
        return [{"task": "Прислать презентацию", "category": "Work", "deadline": "2030-01-10T23:59:00", "priority": "High"},
                {"task": "Купить кабель", "category": "Выдумка", "priority": "Low"},
                {"task": "", "category": "Work"}]
    task_service.parse_text_to_tasks = fake_parse
    before = len(db.tables["tasks"])
    r = client.post("/tasks/preview", headers=hA, json={"text": "созвон: прислать презентацию, купить кабель"})
    items = r.json()
    check("нашёл задачи и ничего не сохранил", r.status_code == 200 and len(items) == 2 and len(db.tables["tasks"]) == before)
    check("неизвестная категория → запасная", items[1]["category"] == "Work")
    r = client.post("/tasks", headers=hA, json={**items[0], "source": "note"})
    check("отмеченная задача создаётся обычным POST /tasks", r.status_code == 201 and r.json()["task"] == "Прислать презентацию")
    check("без входа → 401", client.post("/tasks/preview", json={"text": "x"}).status_code == 401)

    section("/split в боте")
    u = ic.tg_update(A["telegram_user_id"], text="/split созвон: прислать презентацию, купить кабель")
    asyncio.run(h.split_command(u, SimpleNamespace(bot=ic.FakeBot())))
    reply = u.replies[-1]
    buttons = [b for row in reply.markup.inline_keyboard for b in row]
    token = buttons[0].callback_data.split(":")[1]
    check("показывает найденное с галочками и кнопкой «Добавить (2)»",
          "Нашёл задач: 2" in reply.text and buttons[0].text.startswith("☑️") and any(b.text == "Добавить (2)" for b in buttons))
    before = len(db.tables["tasks"])

    def press(user, data):
        q = ic.tg_update(user["telegram_user_id"], callback_data=data)

        async def edit_markup(**_):
            return None
        q.callback_query.edit_message_reply_markup = edit_markup
        asyncio.run(h.split_callback(q, SimpleNamespace(bot=ic.FakeBot())))
        return q
    q = press(B, f"sp:{token}:ok")
    check("чужой выбор не срабатывает", len(db.tables["tasks"]) == before)
    press(A, f"sp:{token}:1")
    q = press(A, f"sp:{token}:ok")
    titles = [x["task"] for x in db.tables["tasks"][before:]]
    check("сняли галочку — добавлена только отмеченная", titles == ["Прислать презентацию"], str(titles))
    q = press(A, f"sp:{token}:ok")
    check("повторное нажатие ничего не дублирует", len(db.tables["tasks"]) == before + 1)
    u = ic.tg_update(A["telegram_user_id"], text="/split")
    asyncio.run(h.split_command(u, SimpleNamespace(bot=ic.FakeBot())))
    check("/split без текста — подсказка", "Пришлите текст" in u.replies[-1].text)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
