"""
Проверки миграции 014: совместные задачи (приглашение, «Принять / Отклонить», общие поля, галочка у обоих,
общие шаги и комментарии, выход при удалении, изоляция), место у задач и заметок, поиск адреса,
галочка у повторяющегося платежа в кошельке.

    python scripts/shared_check.py

Только локально: база в памяти, Telegram, Google и OpenStreetMap подменены.
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
    import money_service
    import notifier
    import places_service
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    Y = users_service.create_user("Yera", 8001)
    J = users_service.create_user("Julie", 8002)
    X = users_service.create_user("Чужой", 8003)
    for u in (Y, J, X):
        ic.give_categories(u, "Work")
        users_service.link_chat(u["telegram_user_id"], u["id"])
        money_service.ensure_defaults(u["id"])
    hY, hJ, hX = ic.key_headers(Y), ic.key_headers(J), ic.key_headers(X)
    sent = []
    notifier.send_telegram = lambda chat_id, text, with_app_button=False, buttons=None: sent.append((chat_id, text, buttons)) or True

    def press(user, data):
        u = ic.tg_update(user["telegram_user_id"], callback_data=data)
        asyncio.run(h.share_callback(u, SimpleNamespace(bot=ic.FakeBot())))
        return u.edits[-1] if u.edits else None

    def copy_of(user, title):
        return next((t for t in db.tables["tasks"] if t["user_id"] == user["id"] and t["task"] == title), None)

    section("Приглашение")
    people = client.get("/people", headers=hY).json()
    check("«С кем» — остальные пользователи, без себя", {p["name"] for p in people} == {"Julie", "Чужой"})
    t = client.post("/tasks", headers=hY, json={"task": "Купить подарок маме", "category": "Work",
                                               "deadline": "2030-01-18T18:00:00", "location": " Stephansplatz 1, Wien "}).json()
    check("место сохраняется у задачи (без лишних пробелов)", t["location"] == "Stephansplatz 1, Wien")
    r = client.post(f"/tasks/{t['id']}/share", headers=hY, json={"user_id": J["id"]})
    check("приглашение отправлено", r.status_code == 200 and r.json()["status"] == "pending", r.text[:200])
    msg = [m for m in sent if m[0] == J["telegram_user_id"]][-1]
    data = msg[2][0][0]["callback_data"]
    check("Julie пришло приглашение с кнопками", "Yera предлагает задачу на двоих" in msg[1] and data.startswith("sh:y:")
          and len(data.encode()) <= 64)
    me = next(x for x in client.get("/tasks", headers=hY).json() if x["id"] == t["id"])
    check("у автора категория «С Julie» и пометка «ждёт Julie»", me["category"] == "С Julie" and me["shared"]["pending"] == ["Julie"])
    check("повторное приглашение не дублируется", client.post(f"/tasks/{t['id']}/share", headers=hY, json={"user_id": J["id"]}).json()["invite_id"]
          == data.split(":")[2])
    check("пригласить себя нельзя", client.post(f"/tasks/{t['id']}/share", headers=hY, json={"user_id": Y["id"]}).status_code == 404)
    check("чужой задачей не поделиться", client.post(f"/tasks/{t['id']}/share", headers=hX, json={"user_id": J["id"]}).status_code == 404)
    rec = client.post("/tasks", headers=hY, json={"task": "Спорт", "category": "Work", "deadline": "2030-01-07T09:00:00",
                                                 "recurrence_pattern": "daily"}).json()
    check("повторяющейся задачей поделиться нельзя (422)",
          client.post(f"/tasks/{rec['id']}/share", headers=hY, json={"user_id": J["id"]}).status_code == 422)

    section("Ответ")
    check("чужой человек не может принять приглашение", "не найдено" in press(X, data) and not copy_of(X, "Купить подарок маме"))
    sent.clear()
    text = press(J, data)
    jt = copy_of(J, "Купить подарок маме")
    check("Julie приняла — у неё своя копия в категории «С Yera»", jt and jt["category"] == "С Yera" and "добавлена" in text
          and jt["deadline"].startswith("2030-01-18T18:00") and jt["location"] == "Stephansplatz 1, Wien")
    check("автору пришло «теперь общая»", any(m[0] == Y["telegram_user_id"] and "теперь общая с Julie" in m[1] for m in sent))
    check("повторное нажатие ничего не дублирует", "уже ответили" in press(J, data)
          and len([x for x in db.tables["tasks"] if x["task"] == "Купить подарок маме"]) == 2)
    me = next(x for x in client.get("/tasks", headers=hY).json() if x["id"] == t["id"])
    check("у автора — «С Julie», без «ждёт»", me["shared"] == {"with": ["Julie"], "pending": []})
    check("у Julie — «С Yera»", next(x for x in client.get("/tasks", headers=hJ).json() if x["id"] == jt["id"])["shared"]["with"] == ["Yera"])

    section("Общие поля, шаги и комментарии")
    client.patch(f"/tasks/{t['id']}", headers=hY, json={"task": "Купить подарок маме и цветы", "deadline": "2030-01-19T17:00:00",
                                                       "note": "бюджет 100 €"})
    jt = next(x for x in db.tables["tasks"] if x["id"] == jt["id"])
    check("название, срок и заметка поменялись и у Julie", jt["task"] == "Купить подарок маме и цветы"
          and jt["deadline"].startswith("2030-01-19T17:00") and jt["note"] == "бюджет 100 €", str((jt["task"], jt["deadline"], jt["note"])))
    client.patch(f"/tasks/{jt['id']}", headers=hJ, json={"category": "Work"})
    check("категория у каждого своя", next(x for x in db.tables["tasks"] if x["id"] == t["id"])["category"] == "С Julie")
    check("повтор у совместной задачи → 422",
          client.patch(f"/tasks/{jt['id']}", headers=hJ, json={"recurrence_pattern": "daily"}).status_code == 422)
    s1 = client.post(f"/tasks/{jt['id']}/subtasks", headers=hJ, json={"content": "Торт"}).json()
    check("шаг, добавленный Julie, виден у Yera под его копией",
          any(s["content"] == "Торт" and s["task_id"] == t["id"] for s in client.get("/subtasks", headers=hY).json()))
    r = client.patch(f"/subtasks/{s1['id']}", headers=hY, json={"is_done": True})
    check("Yera отмечает общий шаг", r.status_code == 200 and r.json()["task_id"] == t["id"])
    check("чужой не видит и не меняет шаг", client.patch(f"/subtasks/{s1['id']}", headers=hX, json={"is_done": False}).status_code == 404
          and not any(s["content"] == "Торт" for s in client.get("/subtasks", headers=hX).json()))
    client.post(f"/tasks/{t['id']}/comments", headers=hY, json={"text": "цветы — тюльпаны"})
    cm = client.get(f"/tasks/{jt['id']}/comments", headers=hJ).json()
    check("комментарий Yera виден Julie с именем автора", len(cm) == 1 and cm[0]["mine"] is False and cm[0]["author"] == "Yera")
    check("счётчик 💬 у обоих", next(x for x in client.get("/tasks", headers=hJ).json() if x["id"] == jt["id"])["comments_count"] == 1)
    check("чужой комментарий удалить нельзя", client.delete(f"/comments/{cm[0]['id']}", headers=hJ).status_code == 404)
    check("чужой не читает комментарии", client.get(f"/tasks/{jt['id']}/comments", headers=hX).status_code == 404)

    section("Галочка у обоих")
    sent.clear()
    client.patch(f"/tasks/{jt['id']}", headers=hJ, json={"status": "Done"})
    yt = next(x for x in db.tables["tasks"] if x["id"] == t["id"])
    check("Julie отметила — закрыто и у Yera", yt["status"] == "Done")
    check("Yera пришло «выполнено (Julie)»", any(m[0] == Y["telegram_user_id"] and "выполнено (Julie)" in m[1] for m in sent))
    check("Julie себе не пишет", not any(m[0] == J["telegram_user_id"] for m in sent))

    section("Выход при удалении")
    t2 = client.post("/tasks", headers=hY, json={"task": "Отпуск", "category": "Work"}).json()
    inv = client.post(f"/tasks/{t2['id']}/share", headers=hY, json={"user_id": J["id"]}).json()["invite_id"]
    press(J, f"sh:y:{inv}")
    j2 = copy_of(J, "Отпуск")
    client.post(f"/tasks/{t2['id']}/subtasks", headers=hY, json={"content": "Билеты"})
    client.post(f"/tasks/{t2['id']}/comments", headers=hY, json={"text": "в июле"})
    sent.clear()
    check("Yera удалил (главная копия) — 204", client.delete(f"/tasks/{t2['id']}", headers=hY).status_code == 204)
    j2 = next(x for x in db.tables["tasks"] if x["id"] == j2["id"])
    check("у Julie задача осталась обычной", j2["shared_id"] is None)
    check("шаги и комментарии переехали к Julie", any(s["content"] == "Билеты" and s["task_id"] == j2["id"]
                                                     for s in client.get("/subtasks", headers=hJ).json())
          and len(client.get(f"/tasks/{j2['id']}/comments", headers=hJ).json()) == 1)
    check("Julie пришло «больше не участвует»", any(m[0] == J["telegram_user_id"] and "больше не участвует" in m[1] for m in sent))
    t3 = client.post("/tasks", headers=hY, json={"task": "Отклоню", "category": "Work"}).json()
    inv = client.post(f"/tasks/{t3['id']}/share", headers=hY, json={"user_id": J["id"]}).json()["invite_id"]
    sent.clear()
    press(J, f"sh:n:{inv}")
    check("отклонила — копии нет, автору сообщение", not copy_of(J, "Отклоню")
          and any(m[0] == Y["telegram_user_id"] and "отклонено" in m[1] for m in sent))
    t4 = client.post("/tasks", headers=hY, json={"task": "Удалю до ответа", "category": "Work"}).json()
    inv = client.post(f"/tasks/{t4['id']}/share", headers=hY, json={"user_id": J["id"]}).json()["invite_id"]
    client.delete(f"/tasks/{t4['id']}", headers=hY)
    check("задачу удалили до ответа — «Принять» ничего не создаёт", "не найдено" in press(J, f"sh:y:{inv}")
          and not copy_of(J, "Удалю до ответа"))

    section("Место")
    body = gcal._event_body({"task": "x", "deadline": "2030-01-07T09:00:00", "location": "Stephansplatz 1"}, "Europe/Rome")
    check("место задачи уходит в событие Google Calendar", body["location"] == "Stephansplatz 1")
    r = client.patch(f"/tasks/{t['id']}", headers=hY, json={"location": None})
    check("место можно убрать", r.json()["location"] is None)
    n = client.post("/notes", headers=hY, json={"content": "кафе", "location": "Café Central, Wien"}).json()
    check("место у заметки", n["location"] == "Café Central, Wien")
    r = client.patch(f"/notes/{n['id']}", headers=hY, json={"location": ""})
    check("у заметки место убирается, текст остаётся", r.json()["location"] is None and r.json()["content"] == "кафе")
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"name": "Stephansplatz", "display_name": "Stephansplatz, Innere Stadt, Wien, Österreich"}]
    places_service.httpx.get = lambda url, **kw: calls.append(kw["params"]["q"]) or Resp()
    places_service.time.sleep = lambda s: None
    r = client.get("/places", headers=hY, params={"q": "stephansplatz wien"})
    check("поиск адреса", r.status_code == 200 and r.json()[0]["name"] == "Stephansplatz")
    client.get("/places", headers=hY, params={"q": "Stephansplatz   Wien"})
    check("повторный поиск — из памяти, без запроса наружу", len(calls) == 1)
    check("короткий запрос — пусто без запроса", client.get("/places", headers=hY, params={"q": "ab"}).json() == [] and len(calls) == 1)
    check("без входа → 401", client.get("/places", params={"q": "wien"}).status_code == 401)

    section("Галочка у платежа в кошельке")
    cat = money_service.list_categories(Y["id"], "expense")[0]["id"]
    today = money_service.today_for(Y["id"])
    auto = client.post("/money/recurring", headers=hY, json={"kind": "expense", "amount": "21", "currency": "EUR", "category_id": cat,
                                                            "frequency": "monthly", "note": "Claude", "anchor_date": today.isoformat()}).json()
    r = client.post(f"/money/recurring/{auto['id']}/pay", headers=hY)
    txs = [x for x in db.tables["money_transactions"] if x.get("recurring_id") == auto["id"]]
    nxt = money_service.advance(money_service.date.fromisoformat(auto["next_run"]), "monthly", auto["day_of_month"]).isoformat()
    check("галочка — оплата записана, срок — следующий месяц", r.status_code == 200 and len(txs) == 1 and r.json()["next_run"] == nxt)
    task_rec = client.post("/money/recurring", headers=hY, json={"kind": "expense", "amount": "500", "currency": "EUR", "category_id": cat,
                                                                "frequency": "monthly", "note": "Жильё", "anchor_date": today.isoformat(),
                                                                "as_task": True}).json()
    client.post(f"/money/recurring/{task_rec['id']}/pay", headers=hY)
    open_t = [x for x in db.tables["tasks"] if x.get("money_recurring_id") == task_rec["id"] and x["status"] != "Done"]
    done_t = [x for x in db.tables["tasks"] if x.get("money_recurring_id") == task_rec["id"] and x["status"] == "Done"]
    check("у платежа «по галочке» закрылась его задача и встала новая",
          len(done_t) == 1 and len(open_t) == 1 and len([x for x in db.tables["money_transactions"] if x.get("recurring_id") == task_rec["id"]]) == 1)
    check("чужой платёж не оплатить", client.post(f"/money/recurring/{auto['id']}/pay", headers=hX).status_code == 404)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
