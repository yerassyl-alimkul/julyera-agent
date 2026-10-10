"""
Проверки: время «с — до» у задач, пять валют с парой «основная + дополнительная», выписка PDF, диктовка в
приложении (POST /voice/transcribe) и «Резервная копия в бот».

    python scripts/extras_check.py

Только локально: база в памяти, Whisper и Telegram подменены, курс постоянный.
"""
import base64
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
    import groq_client
    import main as app_main
    import money_service
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    A = users_service.create_user("Доп A", 6001)
    B = users_service.create_user("Доп B", 6002)
    for u in (A, B):
        ic.give_categories(u, "Work")
        users_service.link_chat(u["telegram_user_id"], u["id"])
        money_service.ensure_defaults(u["id"])
    hA, hB = ic.key_headers(A), ic.key_headers(B)

    section("Время «с — до»")
    r = client.post("/tasks", headers=hA, json={"task": "Пара", "category": "Work", "deadline": "2030-01-07T09:00:00",
                                                "ends_at": "2030-01-07T10:30:00"})
    t = r.json()
    check("задача с «до» сохраняется", r.status_code == 201 and t["ends_at"].startswith("2030-01-07T10:30"), r.text[:200])
    r = client.post("/tasks", headers=hA, json={"task": "x", "category": "Work", "deadline": "2030-01-07T09:00:00",
                                                "ends_at": "2030-01-07T08:00:00"})
    check("«до» раньше начала → 422", r.status_code == 422)
    r = client.post("/tasks", headers=hA, json={"task": "x", "category": "Work", "ends_at": "2030-01-07T08:00:00"})
    check("«до» без даты не сохраняется", r.status_code == 201 and r.json()["ends_at"] is None)
    r = client.patch(f"/tasks/{t['id']}", headers=hA, json={"deadline": "2030-01-08T09:00:00"})
    check("перенесли задачу — «до» едет вместе с ней (1,5 часа)", r.json()["ends_at"].startswith("2030-01-08T10:30"))
    r = client.patch(f"/tasks/{t['id']}", headers=hA, json={"ends_at": None})
    check("«до» можно убрать", r.json()["ends_at"] is None)
    body = gcal._event_body({"task": "x", "deadline": "2030-01-07T09:00:00", "ends_at": "2030-01-07T10:30:00"}, "Europe/Rome")
    check("событие в Google Calendar — ровно до «до»", body["end"]["dateTime"] == "2030-01-07T10:30:00")
    body = gcal._event_body({"task": "x", "deadline": "2030-01-07T09:00:00"}, "Europe/Rome")
    check("без «до» — как раньше, 30 минут", body["end"]["dateTime"] == "2030-01-07T09:30:00")

    section("Пять валют, основная + дополнительная")
    me = client.get("/me", headers=hA).json()
    check("по умолчанию пара € и ₸, в списке пять валют",
          me["default_currency"] == "EUR" and me["second_currency"] == "KZT" and len(me["currencies"]) == 5)
    r = client.patch("/me", headers=hA, json={"second_currency": "USD"})
    check("дополнительная — доллар", r.json()["second_currency"] == "USD")
    check("дополнительная = основная → 422", client.patch("/me", headers=hA, json={"second_currency": "EUR"}).status_code == 422)
    r = client.patch("/me", headers=hA, json={"default_currency": "USD"})
    check("основную сделали такой же, как дополнительная — они меняются местами",
          r.json()["default_currency"] == "USD" and r.json()["second_currency"] == "EUR")
    check("у B своя пара", client.get("/me", headers=hB).json()["second_currency"] == "KZT")
    cat = money_service.list_categories(A["id"], "expense")[0]["id"]
    for cur, amount in (("AZN", "12.00"), ("TRY", "350.00"), ("USD", "5.00")):
        r = client.post("/money/transactions", headers=hA, json={"kind": "expense", "amount": amount, "currency": cur,
                                                                "category_id": cat, "spent_on": "2026-10-05"})
        check(f"трата в {cur} сохраняется", r.status_code == 201, r.text[:120])
    s = client.get("/money/summary", headers=hA, params={"month": "2026-10"}).json()
    check("итоги в основной валюте по курсу (≈)", s["main"] and s["main"]["currency"] == "USD")
    check("в тексте бот узнаёт доллары, манаты и лиры", all(money_service.looks_like_money(x) for x in
          ("кофе 5 долларов", "такси 10 манат", "кебаб 250 лир", "обед 15$", "чай 3 ₼")))
    amount, cur, _ = money_service.parse_applepay_amount("$4.50", "EUR")
    check("Apple Pay: «$4.50» — доллары", cur == "USD" and str(amount) == "4.50")

    section("Выписка PDF")
    r = client.post("/money/statement", headers=hA, json={"from": "2026-10-01", "to": "2026-10-31"})
    check("PDF за период скачивается", r.status_code == 200 and r.headers["content-type"] == "application/pdf"
          and r.content[:4] == b"%PDF")
    check("период больше года → 422",
          client.post("/money/statement", headers=hA, json={"from": "2024-01-01", "to": "2026-10-31"}).status_code == 422)
    check("«по» раньше «с» → 422",
          client.post("/money/statement", headers=hA, json={"from": "2026-10-31", "to": "2026-10-01"}).status_code == 422)
    check("без входа → 401", client.post("/money/statement", json={"from": "2026-10-01", "to": "2026-10-31"}).status_code == 401)
    sent = []

    async def fake_doc(_client, chat_id, filename, content, caption, mime="application/json"):
        sent.append((chat_id, filename, mime, content[:4]))
        return True
    app_main._send_telegram_document = fake_doc
    r = client.post("/money/statement", headers=hA, json={"from": "2026-10-01", "to": "2026-10-31", "send": True})
    check("send — файл уходит в бот своему пользователю", r.status_code == 200 and sent
          and sent[-1][0] == A["telegram_user_id"] and sent[-1][2] == "application/pdf" and sent[-1][3] == b"%PDF")
    import statement_service
    data = statement_service.collect(B["id"], __import__("datetime").date(2026, 10, 1), __import__("datetime").date(2026, 10, 31))
    check("в выписке B нет операций A", data["rows"] == [])

    section("Резервная копия в бот")
    r = client.post("/backup/send", headers=hB)
    check("кнопка «Резервная копия в бот»", r.status_code == 200 and sent[-1][0] == B["telegram_user_id"]
          and sent[-1][2] == "application/json")

    section("Диктовка в приложении")
    heard = []
    groq_client.transcribe_audio = lambda data, filename="voice.ogg": heard.append(filename) or "купить молоко завтра"
    audio = base64.b64encode(b"\x1aE\xdf\xa3" + b"0" * 2000).decode()
    r = client.post("/voice/transcribe", headers=hA, json={"audio": audio, "type": "audio/webm;codecs=opus"})
    check("запись распознаётся тем же Whisper", r.status_code == 200 and r.json()["text"] == "купить молоко завтра"
          and heard[-1] == "voice.webm")
    r = client.post("/voice/transcribe", headers=hA, json={"audio": audio, "type": "audio/mp4"})
    check("iPhone пишет mp4 — отправляем как m4a", heard[-1] == "voice.m4a")
    check("слишком короткая запись → 422",
          client.post("/voice/transcribe", headers=hA, json={"audio": base64.b64encode(b"x" * 50).decode()}).status_code == 422)
    check("испорченная запись → 422", client.post("/voice/transcribe", headers=hA, json={"audio": "@@@@@@@@@@@@"}).status_code == 422)
    check("без входа → 401", client.post("/voice/transcribe", json={"audio": audio}).status_code == 401)
    groq_client.transcribe_audio = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("groq"))
    check("Whisper не ответил → 502", client.post("/voice/transcribe", headers=hA, json={"audio": audio}).status_code == 502)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
