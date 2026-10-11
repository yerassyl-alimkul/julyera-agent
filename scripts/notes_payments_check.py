"""
Проверки миграции 013: повторяющийся платёж «по галочке» (задача «💳 …», галочка записывает операцию и ставит
следующий срок) и фото у заметок.

    python scripts/notes_payments_check.py

Только локально: база в памяти, хранилище файлов в памяти, Google Calendar отключён.
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 200).decode()


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import main as app_main
    import money_service
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    A = users_service.create_user("Плат A", 7001)
    B = users_service.create_user("Плат B", 7002)
    for u in (A, B):
        ic.give_categories(u, "Дом")
        users_service.link_chat(u["telegram_user_id"], u["id"])
        money_service.ensure_defaults(u["id"])
    hA, hB = ic.key_headers(A), ic.key_headers(B)
    today = money_service.today_for(A["id"])
    cat = money_service.list_categories(A["id"], "expense")[0]["id"]

    def open_tasks(rec_id):
        return [t for t in db.tables["tasks"] if t.get("money_recurring_id") == rec_id and t["status"] != "Done"]

    def txs(rec_id):
        return [t for t in db.tables.get("money_transactions", []) if t.get("recurring_id") == rec_id]

    section("Платёж «по галочке»")
    r = client.post("/money/recurring", headers=hA, json={"kind": "expense", "amount": "500", "currency": "EUR",
                                                         "category_id": cat, "frequency": "monthly", "note": "Жильё",
                                                         "anchor_date": today.isoformat(), "as_task": True})
    rec = r.json()
    check("правило сохраняется с as_task", r.status_code == 201 and rec["as_task"] is True, r.text[:200])
    due1 = rec["next_run"]
    ot = open_tasks(rec["id"])
    check("сразу появилась задача на срок платежа", len(ot) == 1 and ot[0]["deadline"].startswith(f"{due1}T23:59")
          and ot[0]["task"] == "💳 Жильё · 500 €", str(ot))
    check("задача видна в GET /tasks", any(t["id"] == ot[0]["id"] for t in client.get("/tasks", headers=hA).json()))
    check("без галочки операций нет", txs(rec["id"]) == [])

    money_service.run_recurring(today_of=lambda _u: money_service.advance(
        money_service.date.fromisoformat(due1), "monthly", 1))
    check("в срок само не списывается, задача не дублируется", txs(rec["id"]) == [] and len(open_tasks(rec["id"])) == 1)

    task1 = open_tasks(rec["id"])[0]
    r = client.patch(f"/tasks/{task1['id']}", headers=hA, json={"status": "Done"})
    t = txs(rec["id"])
    check("галочка → операция 500 € сегодняшним днём", r.status_code == 200 and len(t) == 1
          and t[0]["spent_on"] == today.isoformat() and float(t[0]["amount"]) == 500 and t[0]["kind"] == "expense", str(t))
    rec_now = client.get("/money/recurring", headers=hA).json()[0]
    due2 = money_service.advance(money_service.date.fromisoformat(due1), "monthly", rec_now["day_of_month"]).isoformat()
    check("срок платежа сдвинулся на следующий", rec_now["next_run"] == due2, rec_now["next_run"])
    ot = open_tasks(rec["id"])
    check("новая задача на следующий срок", len(ot) == 1 and ot[0]["deadline"].startswith(due2) and ot[0]["id"] != task1["id"])
    client.patch(f"/tasks/{task1['id']}", headers=hA, json={"status": "Done"})
    check("повторная галочка на закрытой задаче ничего не пишет", len(txs(rec["id"])) == 1)
    import task_service
    stale = dict(ot[0])
    next(x for x in db.tables["tasks"] if x["id"] == stale["id"])["status"] = "Done"  # оба запроса уже закрыли задачу
    money_service.pay_from_task(stale, A["id"])
    money_service.pay_from_task(stale, A["id"])
    check("две одновременные галочки — одна операция", len(txs(rec["id"])) == 2 and len(open_tasks(rec["id"])) == 1)
    check("вторая оплата в тот же день не теряется (дата срока)", len({x["spent_on"] for x in txs(rec["id"])}) == 2)

    before = client.get("/money/recurring", headers=hA).json()[0]["next_run"]
    task3 = open_tasks(rec["id"])[0]
    client.delete(f"/tasks/{task3['id']}", headers=hA)
    after = client.get("/money/recurring", headers=hA).json()[0]["next_run"]
    check("удалили неоплаченную задачу — срок пропущен, без операции, следующая задача есть",
          after > before and len(txs(rec["id"])) == 2 and len(open_tasks(rec["id"])) == 1)

    r = client.patch(f"/money/recurring/{rec['id']}", headers=hA, json={"amount": "620,50"})
    check("поменяли сумму — задача следом", open_tasks(rec["id"])[0]["task"] == "💳 Жильё · 620,50 €")
    client.patch(f"/money/recurring/{rec['id']}", headers=hA, json={"active": False})
    check("пауза — открытая задача убирается", open_tasks(rec["id"]) == [])
    client.patch(f"/money/recurring/{rec['id']}", headers=hA, json={"active": True})
    check("сняли паузу — задача снова есть", len(open_tasks(rec["id"])) == 1)
    r = client.patch(f"/money/recurring/{rec['id']}", headers=hA, json={"as_task": False})
    check("выключили «по галочке» — задачи нет, списывается само", r.json()["as_task"] is False and open_tasks(rec["id"]) == []
          and r.json()["next_run"] >= today.isoformat())
    client.patch(f"/money/recurring/{rec['id']}", headers=hA, json={"as_task": True})
    check("включили обратно — задача появилась", len(open_tasks(rec["id"])) == 1)

    section("Изоляция платежей")
    task4 = open_tasks(rec["id"])[0]
    check("B не закрывает чужую задачу платежа",
          client.patch(f"/tasks/{task4['id']}", headers=hB, json={"status": "Done"}).status_code == 404
          and len(txs(rec["id"])) == 2)
    check("B не меняет чужой платёж",
          client.patch(f"/money/recurring/{rec['id']}", headers=hB, json={"as_task": False}).status_code == 404
          and len(open_tasks(rec["id"])) == 1)

    section("Новая трата с «Повторять» и «по галочке»")
    r = client.post("/money/transactions", headers=hA, json={"kind": "expense", "amount": "9.99", "currency": "EUR",
                                                            "category_id": cat, "note": "Spotify", "repeat": "monthly",
                                                            "as_task": True})
    rid = r.json()["recurring_id"]
    check("первая операция записана, дальше — задачей", r.status_code == 201 and len(txs(rid)) == 1
          and len(open_tasks(rid)) == 1 and open_tasks(rid)[0]["task"] == "💳 Spotify · 9,99 €")

    section("Удаление платежа")
    done_before = [t for t in db.tables["tasks"] if t.get("money_recurring_id") == rec["id"] and t["status"] == "Done"]
    check("удаление платежа", client.delete(f"/money/recurring/{rec['id']}", headers=hA).status_code == 204)
    check("открытая задача удалена, закрытые остались",
          not any(t for t in db.tables["tasks"] if t["task"].startswith("💳 Жильё") and t["status"] != "Done")
          and len([t for t in db.tables["tasks"] if t["task"].startswith("💳 Жильё") and t["status"] == "Done"]) == len(done_before))
    check("операции остались", len([t for t in db.tables["money_transactions"] if (t.get("note") or "") == "Жильё"]) == 2)

    section("Фото у заметок")
    note = client.post("/notes", headers=hA, json={"content": "wish list\n- [ ] bag\n- [x] perfume\nhttps://zara.com/x"}).json()
    r = client.post(f"/notes/{note['id']}/files", headers=hA, json={"file": {"type": "image/png", "data": PNG}})
    photo = r.json()
    check("фото прикрепляется", r.status_code == 201 and photo["url"], r.text[:200])
    notes = client.get("/notes", headers=hA).json()
    check("в списке заметок — фото и текст со списком как есть",
          notes[0]["files"][0]["id"] == photo["id"] and "- [x] perfume" in notes[0]["content"])
    check("PDF к заметке нельзя",
          client.post(f"/notes/{note['id']}/files", headers=hA, json={"file": {"type": "application/pdf", "data": PNG}}).status_code == 422)
    check("B не прикрепляет к чужой заметке",
          client.post(f"/notes/{note['id']}/files", headers=hB, json={"file": {"type": "image/png", "data": PNG}}).status_code == 404)
    check("B не видит чужие фото", all(n["files"] == [] for n in client.get("/notes", headers=hB).json()))
    check("B не удаляет чужое фото", client.delete(f"/note-files/{photo['id']}", headers=hB).status_code == 404)
    bucket = db.storage.buckets["task-files"]
    check("файл лежит в папке пользователя", any(p.startswith(f"{A['id']}/notes/{note['id']}/") for p in bucket))
    check("своё фото удаляется вместе с файлом", client.delete(f"/note-files/{photo['id']}", headers=hA).status_code == 204
          and not any(p.startswith(f"{A['id']}/notes/") for p in bucket))
    client.post(f"/notes/{note['id']}/files", headers=hA, json={"file": {"type": "image/png", "data": PNG}})
    client.delete(f"/notes/{note['id']}", headers=hA)
    check("удалили заметку — фото ушли из хранилища", not any(p.startswith(f"{A['id']}/notes/") for p in bucket)
          and not db.tables.get("note_files"))
    check("без входа → 401", client.post(f"/notes/{note['id']}/files", json={"file": {"type": "image/png", "data": PNG}}).status_code == 401)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
