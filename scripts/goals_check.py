"""
Проверки целей накопления (goals_service, миграция 011): создание, «Отложить» / «Снять», «достигнута» один раз,
«в месяц, чтобы успеть», «Отложено всего» в основной валюте, изоляция, /save в боте, резервная копия.

    python scripts/goals_check.py

Только локально: база в памяти, курс постоянный (1 € = 500,35 ₸), Telegram подменён.
"""
import asyncio
import json
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

    import backup_service
    import goals_service
    import main as app_main
    import notifier
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    A = users_service.create_user("Цели A", 5001)
    B = users_service.create_user("Цели B", 5002)
    for u in (A, B):
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hA, hB = ic.key_headers(A), ic.key_headers(B)
    sent = []
    notifier.send_telegram = lambda chat_id, text, *_, **__: sent.append((chat_id, text)) or True

    section("Создание и список")
    check("целей пока нет", client.get("/money/goals", headers=hA).json() == {"goals": [], "total": None})
    r = client.post("/money/goals", headers=hA, json={"name": "Отпуск", "target": "1500", "currency": "EUR",
                                                     "due_date": "2099-03-01", "initial": "300,50"})
    trip = r.json()
    check("цель создаётся, «уже отложено» учтено", r.status_code == 201 and trip["saved"] == "300.50"
          and trip["target"] == "1500.00" and trip["percent"] == 20 and trip["per_month"], r.text[:200])
    laptop = client.post("/money/goals", headers=hA, json={"name": "Ноутбук", "target": "1000000", "currency": "KZT"}).json()
    check("без срока — нет «в месяц»", laptop["per_month"] is None and laptop["due_date"] is None)
    check("пустое название → 422", client.post("/money/goals", headers=hA, json={"name": " ", "target": "10"}).status_code == 422)
    check("сумма 0 → 422", client.post("/money/goals", headers=hA, json={"name": "x", "target": "0"}).status_code == 422)
    check("странная валюта → 422",
          client.post("/money/goals", headers=hA, json={"name": "x", "target": "1", "currency": "USD"}).status_code == 422)

    section("Отложить и снять")
    move = lambda gid, amount, take=False, hh=hA: client.post(f"/money/goals/{gid}/moves", headers=hh,
                                                               json={"amount": amount, "take": take})
    r = move(laptop["id"], "200000")
    check("отложить 200 000 ₸", r.status_code == 201 and r.json()["saved"] == "200000.00" and r.json()["percent"] == 20)
    r = move(laptop["id"], "250000", take=True)
    check("снять больше отложенного нельзя", r.status_code == 422 and "200000.00" in r.text)
    r = move(laptop["id"], "50000", take=True)
    check("снять 50 000 ₸", r.json()["saved"] == "150000.00")
    hist = client.get(f"/money/goals/{laptop['id']}/moves", headers=hA).json()
    check("история: новые сверху, снятие со знаком минус", [m["amount"] for m in hist] == ["-50000.00", "200000.00"])
    total = client.get("/money/goals", headers=hA).json()["total"]
    check("«Отложено всего» в основной валюте по курсу, с пометкой ≈",
          total["currency"] == "EUR" and total["approx"] is True and total["amount"] == "600.29", str(total))

    section("Цель достигнута")
    sent.clear()
    r = move(trip["id"], "1199,50")
    g = r.json()
    check("дошли до суммы — «достигнута» и одно поздравление в бот",
          g["achieved"] and g["first_achieved"] and len(sent) == 1 and "Отпуск" in sent[0][1])
    r = move(trip["id"], "10")
    check("следующее пополнение не поздравляет снова", r.json()["first_achieved"] is False and len(sent) == 1)
    goals = client.get("/money/goals", headers=hA).json()["goals"]
    check("достигнутые — в конце списка", [x["name"] for x in goals] == ["Ноутбук", "Отпуск"])
    r = client.patch(f"/money/goals/{trip['id']}", headers=hA, json={"target": "3000"})
    check("подняли сумму — цель снова в работе", r.status_code == 200 and r.json()["achieved"] is False)
    move(trip["id"], "2000")
    check("…и снова поздравит, когда дойдём", len(sent) == 2)
    r = client.patch(f"/money/goals/{trip['id']}", headers=hA, json={"currency": "KZT"})
    check("валюту нельзя поменять, когда уже отложено", r.status_code == 422)

    section("Изоляция")
    check("чужие цели не видны", client.get("/money/goals", headers=hB).json()["goals"] == [])
    check("в чужую цель не отложить", move(laptop["id"], "1", hh=hB).status_code == 404)
    check("чужую историю не прочитать", client.get(f"/money/goals/{laptop['id']}/moves", headers=hB).status_code == 404)
    check("чужую цель не изменить", client.patch(f"/money/goals/{laptop['id']}", headers=hB, json={"name": "x"}).status_code == 404)
    check("чужую цель не удалить", client.delete(f"/money/goals/{laptop['id']}", headers=hB).status_code == 404)
    check("без входа → 401", client.get("/money/goals").status_code == 401)

    section("Бот /save")

    def say(user, text):
        u = ic.tg_update(user["telegram_user_id"], text=text)
        asyncio.run(h.save_command(u, SimpleNamespace(bot=ic.FakeBot())))
        return u.replies[-1]
    rep = say(A, "/save")
    check("/save — список целей с прогрессом", "Ноутбук" in rep.text and "15%" in rep.text and "Отпуск" in rep.text)
    rep = say(A, "/save 10000 ноут")
    check("/save 10000 ноут — отложено в цель по части названия", "Отложил 10000.00 KZT" in rep.text and "160000.00" in rep.text)
    rep = say(A, "/save -5000 ноутбук")
    check("/save -5000 — снять", "Снял 5000.00 KZT" in rep.text)
    rep = say(A, "/save 10 евро ноутбук")
    check("валюта не совпадает с целью — отказ, ничего не записано",
          "в EUR" in rep.text and goals_service.get_goal(laptop["id"], A["id"])["saved"] == "155000.00")
    client.post("/money/goals", headers=hA, json={"name": "Ноутбук для жены", "target": "500", "currency": "EUR"})
    rep = say(A, "/save 50 ноут")
    buttons = [b for row in rep.markup.inline_keyboard for b in row]
    check("несколько подходящих целей — спросить кнопками", "В какую цель" in rep.text and len(buttons) == 2)
    rep = say(B, "/save 10 ноутбук")
    check("у B своих целей нет — подсказка, чужие не трогаются", "Целей пока нет" in rep.text)

    section("Резервная копия и удаление")
    _, content, _ = backup_service.build(A["id"])
    data = json.loads(content)
    check("цели и их история попадают в копию", len(data["money_goals"]) == 3 and len(data["money_goal_moves"]) >= 6)
    r = client.delete(f"/money/goals/{laptop['id']}", headers=hA)
    check("удаление цели удаляет и историю", r.status_code == 204
          and not [m for m in db.tables["money_goal_moves"] if m["goal_id"] == laptop["id"]])

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
