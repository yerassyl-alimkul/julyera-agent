"""
Проверки шага 7: /today, утренняя и вечерняя сводка, напоминания с кнопками «Готово» / «Отложить на час»,
перенос невыполненного, /digest on|off, частые вызовы /internal/reminders/run.

    python scripts/digest_check.py

Только локально: настоящий main.py и сервисы, база в памяти, время подставное (digest_service.utc_now),
отправка в Telegram подменена — настоящие сообщения не уходят. Код выхода 1, если хоть одна проверка не прошла.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section
ROME = ZoneInfo("Europe/Rome")


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import digest_service
    import google_calendar_service as gcal
    import main as app_main
    import money_service
    import task_service
    import telegram_handlers as h
    import users_service

    task_service.parse_text_to_tasks = ic.fake_parse
    client = TestClient(app_main.app, raise_server_exceptions=False)
    secret = os.environ["REMINDERS_SECRET"]
    A = users_service.create_user("Тест A", 2001)
    B = users_service.create_user("Тест B", 2002)
    C = users_service.create_user("Без задач", 2003)
    for u in (A, B, C):
        ic.give_categories(u, "Work", "Personal")
        users_service.link_chat(u["telegram_user_id"], u["id"])
    money_service.ensure_defaults(A["id"])

    # «Сейчас» — 8 октября 2026 по Риму; часы меняем по ходу проверок
    day = datetime(2026, 10, 8)
    clock = {"local": day.replace(hour=7, minute=0)}
    digest_service.utc_now = lambda: clock["local"].replace(tzinfo=ROME).astimezone(timezone.utc)

    def at(hh, mm=0, plus_days=0):
        clock["local"] = (day + timedelta(days=plus_days)).replace(hour=hh, minute=mm)

    def add(user, text, deadline, **extra):
        row = {"user_id": user["id"], "task": text, "category": "Work", "priority": "Mid", "deadline": deadline, **extra}
        return db.table("tasks").insert(row).execute().data[0]

    t_overdue = add(A, "Сдать отчёт", "2026-10-06T18:00:00+00:00")
    t_today = add(A, "Созвон", "2026-10-08T14:00:00+00:00", google_event_id="ev-1")
    add(A, "Ранний поезд", "2026-10-09T06:30:00+00:00")
    add(A, "Через неделю", "2026-10-15T10:00:00+00:00")
    t_rec = add(A, "Полить цветы", "2026-10-08T09:00:00+00:00", is_recurring=True, recurrence_pattern="daily")
    t_b = add(B, "Задача B", "2026-10-08T12:00:00+00:00")
    db.table("money_transactions").insert({
        "user_id": A["id"], "kind": "expense", "amount": "12.50", "currency": "EUR",
        "category_id": money_service.list_categories(A["id"], "expense")[0]["id"], "spent_on": "2026-10-07"}).execute()

    sent = []

    async def fake_send(_client, chat_id, text, buttons=None):
        sent.append(SimpleNamespace(chat_id=chat_id, text=text, buttons=buttons))
        return True

    app_main._send_telegram_message = fake_send

    def run():
        start = len(sent)
        r = client.post("/internal/reminders/run", headers={"X-Reminder-Secret": secret})
        assert r.status_code == 200, r.status_code
        return sent[start:]

    def to(user, msgs):
        return [m for m in msgs if m.chat_id == user["telegram_user_id"]]

    gone_events = []
    gcal.sync_task_delete = lambda event_id, user_id: gone_events.append(event_id)

    section("/today")
    at(9, 30)
    u = ic.tg_update(A["telegram_user_id"], text="/today")
    asyncio.run(h.today_command(u, SimpleNamespace(bot=ic.FakeBot())))
    text = u.replies[-1].text
    check("/today: просроченное, сегодня и ближайшие 24 часа",
          all(x in text for x in ("Просрочено", "Сдать отчёт", "Сегодня", "Созвон", "Ближайшие 24 часа", "Ранний поезд")),
          text)
    check("/today: дальние задачи и чужие не показываются", "Через неделю" not in text and "Задача B" not in text)
    check("/today: группировка по категориям", "Work:" in text)
    u = ic.tg_update(C["telegram_user_id"], text="/today")
    asyncio.run(h.today_command(u, SimpleNamespace(bot=ic.FakeBot())))
    check("/today: пустой день — короткая дружелюбная фраза", "задач нет" in u.replies[-1].text)

    section("Расписание и повторы")
    at(7, 50)
    check("до 08:00 ничего не отправляется", run() == [])
    at(8, 5)
    msgs = run()
    morning_a = to(A, msgs)
    check("утренняя сводка в 08:05 по местному времени", len(morning_a) == 1 and "Доброе утро" in morning_a[0].text)
    check("в утренней сводке вчерашние траты одной строкой", "Вчера потрачено: 12.50 EUR" in morning_a[0].text)
    check("сводка только со своими задачами", "Задача B" not in morning_a[0].text and "Задача B" in to(B, msgs)[0].text)
    check("пользователю без задач сводка не приходит", to(C, msgs) == [])
    at(8, 20)
    check("повторный запуск в то же утро ничего не дублирует", run() == [])
    asyncio.run(h.digest_command(ic.tg_update(B["telegram_user_id"], text="/digest off"), SimpleNamespace(bot=ic.FakeBot())))
    check("/digest off выключает сводки", users_service.get_by_id(B["id"])["digest_enabled"] is False)

    at(13, 10)
    msgs = run()
    rem = to(A, msgs)
    check("напоминание в 13:10 с кнопками на каждую задачу", len(rem) == 1 and rem[0].buttons
          and [b["callback_data"] for b in rem[0].buttons[0]] == [f"done:{t_overdue['id']}", f"snooze:{t_overdue['id']}"])
    check("напоминания приходят и при выключенных сводках", len(to(B, msgs)) == 1)
    check("callback_data не длиннее 64 байт", all(len(b["callback_data"].encode()) <= 64
                                                 for m in msgs for row in (m.buttons or []) for b in row))
    check("второй вызов в том же окне ничего не шлёт", run() == [])

    section("Кнопки")
    ctx = SimpleNamespace(bot=ic.FakeBot())
    keyboard = SimpleNamespace(inline_keyboard=[[SimpleNamespace(**b) for b in row] for row in rem[0].buttons])

    def press(user, data, message_text=None):
        upd = ic.tg_update(user["telegram_user_id"], callback_data=data)
        upd.callback_query.message = SimpleNamespace(text=message_text or rem[0].text, reply_markup=keyboard)
        edits = []

        async def edit_message_text(t, reply_markup=None, **_):
            edits.append((t, reply_markup))
        upd.callback_query.edit_message_text = edit_message_text
        handler = h.digest_move_callback if data.startswith("dmove:") else h.reminder_callback
        asyncio.run(handler(upd, ctx))
        return upd.answers, edits

    answers, edits = press(B, f"done:{t_today['id']}")
    check("чужой task_id в кнопке ничего не меняет", answers == ["Задача не найдена"] and not edits
          and task_service.get_task(t_today["id"], A["id"])["status"] != "Done")
    answers, edits = press(A, f"done:{t_today['id']}")
    t_after = task_service.get_task(t_today["id"], A["id"])
    remaining = [b.callback_data for row in (edits[-1][1].inline_keyboard if edits[-1][1] else []) for b in row]
    check("«Готово» закрывает задачу", t_after["status"] == "Done")
    check("«Готово» обновляет календарь (событие удалено)", "ev-1" in gone_events)
    check("«Готово»: сообщение дополнено, кнопки этой задачи убраны",
          "✅ Выполнено: Созвон" in edits[-1][0] and not any(t_today["id"] in c for c in remaining) and remaining)
    answers, _ = press(A, f"done:{t_today['id']}")
    check("повторное «Готово» безопасно", answers == ["Уже выполнено"])
    press(A, f"done:{t_rec['id']}")
    copies = [t for t in db.tables["tasks"] if t["task"] == "Полить цветы" and t["status"] != "Done"]
    check("«Готово» у повторяющейся создаёт следующую (тот же путь, что у веба)",
          len(copies) == 1 and copies[0]["deadline"].startswith("2026-10-09"))

    answers, edits = press(A, f"snooze:{t_overdue['id']}")
    row = next(t for t in db.tables["tasks"] if t["id"] == t_overdue["id"])
    until = digest_service._aware_utc(row["snoozed_until"]).astimezone(ROME)
    check("«Отложить» ставит snoozed_until = сейчас + 1 час", until.strftime("%H:%M") == "14:10")
    check("«Отложить»: «Отложено до 14:10» в сообщении", "⏰ Отложено до 14:10: Сдать отчёт" in edits[-1][0])
    at(14, 0)
    check("до срока отложенная задача не напоминается", run() == [])
    at(14, 15)
    msgs = to(A, run())
    check("после срока — отдельное напоминание с кнопками только об этой задаче",
          len(msgs) == 1 and "Сдать отчёт" in msgs[0].text and len(msgs[0].buttons) == 1)
    at(14, 30)
    check("отложенная не напоминается второй раз", to(A, run()) == [])
    press(A, f"snooze:{t_overdue['id']}")  # снова на час: до 15:30
    at(17, 5)
    msgs = to(A, run())
    check("в 17:05 плановое напоминание, отложенная в нём один раз, без дубля отдельным сообщением",
          len(msgs) == 1 and msgs[0].text.count("Сдать отчёт") == 1)

    section("Вечер")
    at(20, 10)
    msgs = to(A, run())
    ev = msgs[0] if msgs else SimpleNamespace(text="", buttons=None)
    check("вечерняя сводка: выполнено, не закрыто, завтра",
          "Итоги дня" in ev.text and "Выполнено: 2" in ev.text and "Не закрыто" in ev.text and "Завтра" in ev.text, ev.text)
    check("под вечерней сводкой кнопка «Перенести невыполненное»",
          ev.buttons and ev.buttons[0][0]["callback_data"] == "dmove:2026-10-08")
    check("вечером сводки нет у того, кто выключил /digest", to(B, run()) == [])
    answers, edits = press(A, "dmove:2026-10-08", message_text=ev.text)
    moved = next(t for t in db.tables["tasks"] if t["id"] == t_overdue["id"])
    check("«Перенести» ставит невыполненное на завтра в то же время",
          moved["deadline"].startswith("2026-10-09T18:00") and "Перенесено на завтра" in edits[-1][0])
    check("задача B не тронута переносом", next(t for t in db.tables["tasks"] if t["id"] == t_b["id"])["deadline"].startswith("2026-10-08"))
    answers, _ = press(A, "dmove:2026-10-08", message_text=ev.text)
    check("повторный перенос безопасен", answers == ["Уже перенесено"])
    at(9, 0, plus_days=1)
    answers, _ = press(A, "dmove:2026-10-08", message_text=ev.text)
    check("перенос из вчерашней сводки отклоняется", answers == ["Эта сводка уже устарела"])

    section("Задача на открытый день")
    hA = {"X-Access-Key": A["access_key"]}
    r = client.post("/tasks/parse", headers=hA, json={"text": "Позвонить врачу", "default_date": "2026-10-15"})
    check("задача без даты в тексте встаёт на открытый в приложении день",
          r.status_code == 201 and r.json()[0]["deadline"].startswith("2026-10-15T23:59"), r.text[:120])
    r = client.post("/tasks/parse", headers=hA, json={"text": "Без дня"})
    check("без открытого дня задача остаётся без даты", r.status_code == 201 and r.json()[0]["deadline"] is None)
    r = client.post("/tasks/parse", headers=hA, json={"text": "Кривой день", "default_date": "15.10.2026"})
    check("неверная дата по умолчанию просто игнорируется", r.status_code == 201 and r.json()[0]["deadline"] is None)

    section("Панель кнопок и помощь")
    tasks_before = len(db.tables["tasks"])
    u = ic.tg_update(A["telegram_user_id"], text=h.BTN_TODAY)
    asyncio.run(h.handle_text(u, ctx))
    check("кнопка «📋 Сегодня» показывает план и не создаёт задачу",
          len(db.tables["tasks"]) == tasks_before and u.replies and ("План" in u.replies[-1].text or "На " in u.replies[-1].text))
    u = ic.tg_update(A["telegram_user_id"], text=h.BTN_LAST)
    asyncio.run(h.handle_text(u, ctx))
    check("кнопка «🧾 Последние траты» показывает последние операции", len(db.tables["tasks"]) == tasks_before and u.replies)
    u = ic.tg_update(A["telegram_user_id"], text="/help")
    asyncio.run(h.help_command(u, ctx))
    help_buttons = [b.callback_data for row in u.replies[0].markup.inline_keyboard for b in row if b.callback_data]
    panel = [b.text for row in u.replies[1].markup.keyboard for b in row]
    check("/help: команды по группам и кнопки под ними",
          all(x in u.replies[0].text for x in ("/today", "/month", "/currency", "/applepay", "/digest")) and "h:month" in help_buttons)
    check("/help показывает панель кнопок под полем ввода",
          panel == [h.BTN_TODAY, h.BTN_TASKS, h.BTN_MONTH, h.BTN_LAST, h.BTN_HELP])
    check("callback_data помощи не длиннее 64 байт", all(len(c.encode()) <= 64 for c in help_buttons))
    replies = []

    async def reply_text(t, **kw):
        replies.append(t)
    upd = ic.tg_update(A["telegram_user_id"], callback_data="h:month")
    upd.callback_query.message = SimpleNamespace(text="", reply_text=reply_text, chat=SimpleNamespace(send_action=None))
    asyncio.run(h.help_callback(upd, ctx))
    check("кнопка под /help выполняет команду («Месяц»)", replies and ("Октябрь" in replies[-1] or "операций" in replies[-1]), str(replies[-1:]))
    upd = ic.tg_update(A["telegram_user_id"], callback_data="h:currency")
    upd.callback_query.message = SimpleNamespace(text="", reply_text=reply_text, chat=SimpleNamespace(send_action=None))
    asyncio.run(h.help_callback(upd, ctx))
    check("кнопка «Валюта» показывает текущую валюту", "Валюта по умолчанию" in replies[-1])
    u = ic.tg_update(A["telegram_user_id"], text="/applepay")
    asyncio.run(h.applepay_command(u, ctx))
    texts = [r.text for r in u.replies]
    check("/applepay: инструкция на русском и английском, адрес, ключ отдельным сообщением",
          len(texts) == 4 and "«Wallet»" in texts[0] and "Wallet (older iOS: Transaction)" in texts[1]
          and "money/applepay" in texts[0] and texts[3] == A["access_key"])
    u = ic.tg_update(9_999_998, text="/help")
    asyncio.run(h.help_command(u, ctx))
    check("незнакомому /help — «Нет доступа»", "Нет доступа" in u.replies[-1].text)

    section("Устойчивость")
    at(13, 5, plus_days=1)
    real = digest_service.due_messages

    def flaky(user, now_utc=None):
        if user["id"] == A["id"]:
            raise RuntimeError("сбой")
        return real(user, now_utc)
    digest_service.due_messages = flaky
    try:
        msgs = run()
    finally:
        digest_service.due_messages = real
    check("сбой у одного пользователя не мешает остальным", to(B, msgs) and not to(A, msgs))
    import time as _time
    for i in range(30):
        users_service.create_user(f"Нагрузочный {i}", 5000 + i)
    at(13, 30, plus_days=1)
    started = _time.time()
    run()
    check("частый вызов быстрый (меньше 10 секунд на 30+ пользователей)", _time.time() - started < 10)
    check("без секрета → 403", client.post("/internal/reminders/run").status_code == 403)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
