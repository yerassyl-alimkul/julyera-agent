"""
Проверки администрирования (миграция 016): владелец видит людей и их задачи (только просмотр), отключает и
возвращает доступ; отключённый не входит в приложение и бота и не получает сообщений. Приоритета в строках
задач бота и в событиях календаря больше нет.

    python scripts/admin_check.py

Только локально: база в памяти, Telegram подменён.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import google_calendar_service as gcal
    import main as app_main
    import notifier
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    sent = []
    notifier.send_telegram = lambda chat_id, text, with_app_button=False, buttons=None: sent.append(chat_id) or True
    O = users_service.create_user("Yera", 9101)
    next(u for u in db.tables["users"] if u["id"] == O["id"])["is_admin"] = True
    J = users_service.create_user("Julie", 9102)
    for u in (O, J):
        ic.give_categories(u, "Work")
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hO, hJ = ic.key_headers(O), ic.key_headers(J)
    t = client.post("/tasks", headers=hJ, json={"task": "Купить хлеб", "category": "Work", "deadline": "2020-01-01T10:00:00"}).json()
    client.post(f"/tasks/{t['id']}/subtasks", headers=hJ, json={"content": "Батон"})
    client.post("/notes", headers=hJ, json={"content": "личная заметка"})

    section("Только владелец")
    check("не владелец → 403", client.get("/admin/users", headers=hJ).status_code == 403)
    check("без входа → 401", client.get("/admin/users").status_code == 401)
    users = client.get("/admin/users", headers=hO).json()
    jul = next(u for u in users if u["name"] == "Julie")
    check("список людей со сводкой (просрочено, открыто)", users[0]["me"] and jul["open"] == 1 and jul["overdue"] == 1)
    data = client.get(f"/admin/users/{J['id']}/tasks", headers=hO).json()
    check("задачи и шаги человека видны", [x["task"] for x in data["tasks"]] == ["Купить хлеб"]
          and data["subtasks"][0]["content"] == "Батон" and data["subtasks"][0]["task_id"] == t["id"])
    check("заметок в выдаче нет", "notes" not in data and "личная заметка" not in str(data))
    check("не владелец чужие задачи не смотрит", client.get(f"/admin/users/{O['id']}/tasks", headers=hJ).status_code == 403)
    check("менять чужие задачи владелец не может", client.patch(f"/tasks/{t['id']}", headers=hO, json={"task": "x"}).status_code == 404)

    section("Отключить доступ")
    check("себя отключить нельзя", client.post(f"/admin/users/{O['id']}/block", headers=hO, json={"blocked": True}).status_code == 422)
    r = client.post(f"/admin/users/{J['id']}/block", headers=hO, json={"blocked": True})
    check("отключили Julie", r.status_code == 200 and r.json()["blocked"] is True)
    check("Julie не входит в приложение (экран ожидания)", client.get("/tasks", headers=hJ).status_code == 403
          and client.get("/tasks", headers=hJ).json()["detail"]["code"] == "not_approved")
    u = ic.tg_update(J["telegram_user_id"], text="/start")
    asyncio.run(h.start(u, SimpleNamespace(bot=ic.FakeBot(), args=[])))
    check("бот отвечает «Доступ закрыт», запрос владельцу не шлёт", "закрыт" in u.replies[-1].text)
    sent.clear()
    notifier.notify_user(J["id"], "привет")
    check("сообщения отключённому не уходят", sent == [])
    check("в рассылки не попадает", all(x["id"] != J["id"] for x in users_service.list_users()))
    check("в админке виден с пометкой", next(x for x in client.get("/admin/users", headers=hO).json() if x["name"] == "Julie")["blocked"])
    client.post(f"/admin/users/{J['id']}/block", headers=hO, json={"blocked": False})
    check("вернули доступ — снова входит, задачи на месте", client.get("/tasks", headers=hJ).status_code == 200
          and len(client.get("/tasks", headers=hJ).json()) == 1)

    section("Без приоритета")
    check("в событии Google Calendar нет значка приоритета",
          gcal._event_body({"task": "x", "priority": "High", "deadline": "2030-01-07T09:00:00"}, "Europe/Rome")["summary"] == "x")
    check("в строке задачи бота нет значка приоритета", "🔴" not in h._format_task_line({"task": "x", "priority": "High"}))

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
