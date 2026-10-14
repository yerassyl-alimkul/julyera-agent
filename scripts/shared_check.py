"""
Проверки миграции 014: группы с общими задачами (вступление по ссылке, задачи сразу у всех, общие поля, шаги и
комментарии, галочка у всех или у каждого своя, права, выход), место у задач и заметок, поиск адреса,
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
    import groups_service
    groups_service.bot_username = lambda: "JulYeraTestBot"
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

    def join(user, group):
        token = group["invite"]["start"].split("g_")[1]
        u = ic.tg_update(user["telegram_user_id"], text=group["invite"]["start"])
        asyncio.run(h.start(u, SimpleNamespace(bot=ic.FakeBot(), args=[f"g_{token}"])))
        cu = ic.tg_update(user["telegram_user_id"], callback_data=f"gj:y:{token}")
        asyncio.run(h.group_join_callback(cu, SimpleNamespace(bot=ic.FakeBot())))
        return u, cu

    section("Группа и вступление по ссылке")
    check("групп пока нет", client.get("/groups", headers=hY).json() == [])
    check("пустое название → 422", client.post("/groups", headers=hY, json={"name": " "}).status_code == 422)
    pair = client.post("/groups", headers=hY, json={"name": "Мы с Julie", "tick_all": True}).json()
    check("группа создана, создатель — участник", pair["owner"] and [m["name"] for m in pair["members"]] == ["Yera"]
          and pair["invite"]["url"] == f"https://t.me/JulYeraTestBot?start={pair['invite']['start'].split()[1]}")
    check("чужой группу не видит", client.get(f"/groups/{pair['id']}", headers=hX).status_code == 404)
    t = client.post("/tasks", headers=hY, json={"task": "Купить подарок маме", "category": "Work", "group_id": pair["id"],
                                               "deadline": "2030-01-18T18:00:00", "location": " Stephansplatz 1, Wien "}).json()
    check("задача группы: категория — название группы, место без лишних пробелов",
          t["category"] == "Мы с Julie" and t["group_id"] == pair["id"] and t["location"] == "Stephansplatz 1, Wien", str(t)[:200])
    check("чужой в группу задачу не добавит", client.post("/tasks", headers=hX, json={"task": "x", "category": "Work",
                                                                                     "group_id": pair["id"]}).status_code == 404)
    check("задачу группы нельзя сделать повторяющейся",
          client.post("/tasks", headers=hY, json={"task": "x", "category": "Work", "group_id": pair["id"],
                                                  "deadline": "2030-01-07T09:00:00", "recurrence_pattern": "daily"}).status_code == 422)
    sent.clear()
    u, cu = join(J, pair)
    check("Julie открыла ссылку — «Вступить?»", "зовёт вас в группу «Мы с Julie»" in u.replies[-1].text
          and u.replies[-1].markup.inline_keyboard[0][0].callback_data.startswith("gj:y:"))
    jt = copy_of(J, "Купить подарок маме")
    check("вступила — получила открытые задачи группы в категории группы", jt and jt["category"] == "Мы с Julie"
          and jt["group_id"] == pair["id"] and jt["location"] == "Stephansplatz 1, Wien" and "Добавил открытые задачи группы: 1" in cu.edits[-1])
    check("Yera пришло «теперь в группе»", any(m[0] == Y["telegram_user_id"] and "теперь в группе" in m[1] for m in sent))
    check("повторное нажатие ничего не дублирует", "уже в группе" in join(J, pair)[1].edits[-1]
          and len([x for x in db.tables["tasks"] if x["task"] == "Купить подарок маме"]) == 2)
    check("группу видят оба", [g["name"] for g in client.get("/groups", headers=hJ).json()] == ["Мы с Julie"]
          and client.get("/groups", headers=hJ).json()[0]["owner"] is False)
    sent.clear()
    t2 = client.post("/tasks", headers=hJ, json={"task": "Забронировать столик", "category": "что угодно", "group_id": pair["id"]}).json()
    check("Julie тоже добавляет — у Yera задача появилась сразу, без приглашения",
          copy_of(Y, "Забронировать столик") is not None and copy_of(Y, "Забронировать столик")["category"] == "Мы с Julie")
    check("Yera пришло «новая задача от Julie»", any(m[0] == Y["telegram_user_id"] and "новая задача от Julie" in m[1] for m in sent))
    me = next(x for x in client.get("/tasks", headers=hY).json() if x["id"] == t["id"])
    check("в списке задач — группа и с кем", me["shared"]["group"]["name"] == "Мы с Julie" and me["shared"]["with"] == ["Julie"]
          and me["shared"]["tick_all"] is True)

    section("Общие поля, шаги и комментарии")
    client.patch(f"/tasks/{t['id']}", headers=hY, json={"task": "Купить подарок маме и цветы", "deadline": "2030-01-19T17:00:00",
                                                       "note": "бюджет 100 €"})
    jt = next(x for x in db.tables["tasks"] if x["id"] == jt["id"])
    check("название, срок и заметка поменялись и у Julie", jt["task"] == "Купить подарок маме и цветы"
          and jt["deadline"].startswith("2030-01-19T17:00") and jt["note"] == "бюджет 100 €", str((jt["task"], jt["deadline"], jt["note"])))
    check("повтор у задачи группы → 422",
          client.patch(f"/tasks/{jt['id']}", headers=hJ, json={"recurrence_pattern": "daily"}).status_code == 422)
    s1 = client.post(f"/tasks/{jt['id']}/subtasks", headers=hJ, json={"content": "Торт"}).json()
    client.post(f"/tasks/{jt['id']}/subtasks", headers=hJ, json={"content": "Цветы"})  # второй шаг открыт — задача не закроется сама
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

    section("Галочка у всех (tick_all)")
    sent.clear()
    client.patch(f"/tasks/{jt['id']}", headers=hJ, json={"status": "Done"})
    check("Julie отметила — закрыто и у Yera", next(x for x in db.tables["tasks"] if x["id"] == t["id"])["status"] == "Done")
    check("Yera пришло «выполнено (Julie)»", any(m[0] == Y["telegram_user_id"] and "выполнено (Julie)" in m[1] for m in sent))

    section("Группа, где у каждого своя галочка")
    M = users_service.create_user("Мама", 8004)
    ic.give_categories(M, "Дом")
    users_service.link_chat(M["telegram_user_id"], M["id"])
    hM = ic.key_headers(M)
    fam = client.post("/groups", headers=hY, json={"name": "Семья", "tick_all": False}).json()
    join(J, fam)
    join(M, fam)
    check("в группе трое", len(client.get(f"/groups/{fam['id']}", headers=hM).json()["members"]) == 3)
    gt = client.post("/tasks", headers=hM, json={"task": "Сдать документы", "category": "Дом", "group_id": fam["id"]}).json()
    check("задача мамы появилась у всех в «Семья»", all(copy_of(u, "Сдать документы")["category"] == "Семья" for u in (Y, J, M)))
    sent.clear()
    client.patch(f"/tasks/{copy_of(J, 'Сдать документы')['id']}", headers=hJ, json={"status": "Done"})
    check("галочка у каждого своя — у Yera и мамы открыта",
          copy_of(Y, "Сдать документы")["status"] != "Done" and copy_of(M, "Сдать документы")["status"] != "Done")
    check("остальным — «Julie — готово (1 из 3)»", sum(1 for m in sent if "Julie — готово (1 из 3)" in m[1]) == 2)
    info = next(x for x in client.get("/tasks", headers=hY).json() if x["task"] == "Сдать документы")["shared"]
    check("у Yera «1 из 3 готово»", info["tick_all"] is False and info["done"] == 1 and info["total"] == 3, str(info))

    section("Права в группе")
    check("переименовать может только создатель", client.patch(f"/groups/{fam['id']}", headers=hJ, json={"name": "x"}).status_code == 403)
    r = client.patch(f"/groups/{fam['id']}", headers=hY, json={"name": "Родные"})
    check("переименовали — категория у участников тоже", r.json()["name"] == "Родные" and copy_of(M, "Сдать документы")["category"] == "Родные")
    check("создатель выйти не может", client.post(f"/groups/{fam['id']}/leave", headers=hY).status_code == 422)
    sent.clear()
    check("мама вышла", client.post(f"/groups/{fam['id']}/leave", headers=hM).status_code == 204)
    check("её открытые копии удалены, у остальных задача осталась", copy_of(M, "Сдать документы") is None
          and copy_of(Y, "Сдать документы") is not None)
    check("остальным — «больше не в группе», без писем по каждой задаче",
          sum(1 for m in sent if "больше не в группе" in m[1]) == 2 and not any("больше не участвует" in m[1] for m in sent))
    check("выйти из чужой группы нельзя", client.post(f"/groups/{fam['id']}/leave", headers=hX).status_code == 404)
    check("убрать участника может создатель", client.delete(f"/groups/{fam['id']}/members/{J['id']}", headers=hY).status_code == 204
          and len(client.get(f"/groups/{fam['id']}", headers=hY).json()["members"]) == 1)
    check("участник убрать не может", client.delete(f"/groups/{pair['id']}/members/{Y['id']}", headers=hJ).status_code == 403)

    section("Выход из задачи при удалении")
    t3 = client.post("/tasks", headers=hY, json={"task": "Отпуск", "category": "Work", "group_id": pair["id"]}).json()
    j3 = copy_of(J, "Отпуск")
    client.post(f"/tasks/{t3['id']}/subtasks", headers=hY, json={"content": "Билеты"})
    client.post(f"/tasks/{t3['id']}/comments", headers=hY, json={"text": "в июле"})
    sent.clear()
    check("Yera удалил (главная копия) — 204", client.delete(f"/tasks/{t3['id']}", headers=hY).status_code == 204)
    j3 = next(x for x in db.tables["tasks"] if x["id"] == j3["id"])
    check("у Julie задача осталась (без связи)", j3["shared_id"] is None)
    check("шаги и комментарии переехали к Julie", any(s["content"] == "Билеты" and s["task_id"] == j3["id"]
                                                     for s in client.get("/subtasks", headers=hJ).json())
          and len(client.get(f"/tasks/{j3['id']}/comments", headers=hJ).json()) == 1)
    check("Julie пришло «больше не участвует»", any(m[0] == J["telegram_user_id"] and "больше не участвует" in m[1] for m in sent))
    sent.clear()
    check("удалить группу может только создатель", client.delete(f"/groups/{pair['id']}", headers=hJ).status_code == 403)
    check("группа удалена — задачи остались обычными", client.delete(f"/groups/{pair['id']}", headers=hY).status_code == 204
          and copy_of(J, "Забронировать столик")["group_id"] is None and client.get("/groups", headers=hJ).json() == []
          and any(m[0] == J["telegram_user_id"] and "Группу «Мы с Julie» удалили" in m[1] for m in sent))

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
