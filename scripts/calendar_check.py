"""
Проверки синхронизации Google Calendar → задачи (calendar_sync.py) и обратных правок импортированных событий.

    python scripts/calendar_check.py

Только локально: настоящий main.py и сервисы, база в памяти, Google Calendar и Groq подменены (у каждого
пользователя свой календарь в памяти). Код выхода 1, если хоть одна проверка не прошла.
"""
import itertools
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section
ROME = ZoneInfo("Europe/Rome")
ALMATY = ZoneInfo("Asia/Almaty")


def _ts(value: dict) -> datetime:
    if value.get("dateTime"):
        dt = datetime.fromisoformat(value["dateTime"])
        return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(value.get("timeZone") or "Europe/Rome"))
    return datetime.fromisoformat(value["date"]).replace(tzinfo=ROME)


class FakeCalendar:
    """Календарь одного пользователя в памяти, с поведением Google: updated меняется при каждой правке,
    удалённое событие остаётся со status = cancelled, list понимает singleEvents/timeMin/timeMax/страницы."""
    _ids = itertools.count(1)

    def __init__(self):
        self.events, self.calls = {}, []
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def stamp(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat().replace("+00:00", "Z")

    def add(self, event_id, summary, start, end, **extra):
        self.events[event_id] = {"id": event_id, "summary": summary, "start": start, "end": end,
                                 "status": "confirmed", "updated": self.stamp(), **extra}
        return self.events[event_id]

    def edit(self, event_id, **fields):
        self.events[event_id].update(fields, updated=self.stamp())

    # --- API
    def list(self, calendarId, showDeleted, maxResults, pageToken, singleEvents, timeMin, timeMax=None):
        self.calls.append(("list", singleEvents))
        lo, hi = datetime.fromisoformat(timeMin), timeMax and datetime.fromisoformat(timeMax)
        items = []
        for ev in self.events.values():
            if singleEvents and ev.get("recurrence"):
                continue
            if not singleEvents and ev.get("recurringEventId"):
                continue
            if ev["status"] == "cancelled" and not showDeleted:
                continue
            if _ts(ev["end"]) <= lo or (hi and _ts(ev["start"]) >= hi):
                continue
            items.append(dict(ev))
        start = int(pageToken or 0)
        page = items[start:start + maxResults]
        nxt = str(start + maxResults) if start + maxResults < len(items) else None
        return SimpleNamespace(execute=lambda: {"items": page, "nextPageToken": nxt})

    def get(self, calendarId, eventId):
        def run():
            self.calls.append(("get", eventId))
            if eventId not in self.events:
                import httplib2
                from googleapiclient.errors import HttpError
                raise HttpError(httplib2.Response({"status": 404}), b'{"error": "not found"}')
            return dict(self.events[eventId])
        return SimpleNamespace(execute=run)

    def patch(self, calendarId, eventId, body, sendUpdates=None):
        def run():
            self.calls.append(("patch", eventId, sendUpdates, body))
            ev = self.events[eventId]
            for key, value in body.items():
                ev[key] = {k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value
            ev["updated"] = self.stamp()
            return dict(ev)
        return SimpleNamespace(execute=run)

    def insert(self, calendarId, body):
        def run():
            event_id = f"jy{next(self._ids)}"
            self.calls.append(("insert", event_id))
            return dict(self.add(event_id, body["summary"], body["start"], body["end"],
                                 **{k: v for k, v in body.items() if k not in ("summary", "start", "end")}))
        return SimpleNamespace(execute=run)

    def update(self, calendarId, eventId, body):
        def run():
            self.calls.append(("update", eventId))
            self.events[eventId] = {**body, "id": eventId, "status": "confirmed", "updated": self.stamp()}
            return dict(self.events[eventId])
        return SimpleNamespace(execute=run)

    def delete(self, calendarId, eventId, sendUpdates=None):
        def run():
            self.calls.append(("delete", eventId, sendUpdates))
            self.edit(eventId, status="cancelled")
        return SimpleNamespace(execute=run)


def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError
    return HttpError(httplib2.Response({"status": status}), b'{"error": "x"}')


class FakeAccount:
    """Google-аккаунт: несколько календарей (по calendarId) и список календарей (calendarList)."""

    def __init__(self):
        self.cals = {"primary": FakeCalendar()}
        self.readonly, self.list_forbidden, self.list_items = set(), False, []

    def events(self):
        account = self

        class Dispatch:
            def __getattr__(self, name):
                def call(**kw):
                    cal = kw.get("calendarId")
                    if cal not in account.cals:
                        return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(_http_error(404)))
                    if cal in account.readonly and name in ("patch", "delete", "update", "insert"):
                        return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(_http_error(403)))
                    return getattr(account.cals[cal], name)(**kw)
                return call
        return Dispatch()

    def calendarList(self):
        def run():
            if self.list_forbidden:
                raise _http_error(403)
            return {"items": self.list_items}
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=run))


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import calendar_sync
    import google_calendar_service as gcal
    import groq_client
    import main as app_main
    import notifier
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    A = users_service.create_user("Календарь A", 3001)
    B = users_service.create_user("Календарь B", 3002)
    C = users_service.create_user("Без календаря", 3003)
    ic.give_categories(A, "Работа", "Учёба", "Личное")
    ic.give_categories(B, "Общее")
    ic.give_categories(C, "Общее")
    for u in (A, B, C):
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hA, hB = ic.key_headers(A), ic.key_headers(B)

    future = "2030-01-01T00:00:00+00:00"
    db.tables["google_credentials"] = [
        {"user_id": A["id"], "access_token": "tok-A", "refresh_token": "r-A", "token_expiry": future, "needs_reconnect": False},
        {"user_id": B["id"], "access_token": "tok-B", "refresh_token": "r-B", "token_expiry": future, "needs_reconnect": False},
    ]
    cals = {"tok-A": FakeAccount(), "tok-B": FakeAccount()}
    accA = cals["tok-A"]
    calA, calB = cals["tok-A"].cals["primary"], cals["tok-B"].cals["primary"]
    gcal.build = lambda *_, credentials=None, **__: cals[credentials.token]
    gcal._refresh = lambda creds: None
    titles_seen = []

    def fake_classify(titles, names):
        titles_seen.append(list(titles))
        return ["Учёба" if "лекция" in t.lower() else "Работа" if "созвон" in t.lower() else "Выдумка" for t in titles]
    groq_client.classify_event_titles = fake_classify
    sent = []
    notifier.send_telegram = lambda chat_id, text, *_, **__: sent.append((chat_id, text)) or True

    async def fake_send(client, chat_id, text, buttons=None):
        sent.append((chat_id, text))
        return True
    app_main._send_telegram_message = fake_send

    today = datetime.now(ROME).date()
    d = lambda n: today + timedelta(days=n)
    at = lambda day, h, m=0, tz=ROME: {"dateTime": datetime(day.year, day.month, day.day, h, m, tzinfo=tz).isoformat()}

    def tasks_of(user):
        return [t for t in db.tables["tasks"] if t["user_id"] == user["id"]]

    def by_event(user, event_id):
        return next((t for t in tasks_of(user) if t.get("google_event_id") == event_id), None)

    def naive(value):
        return datetime.fromisoformat(value).replace(tzinfo=None)

    # --------------------------------------------------------------- первый перенос
    section("Первый перенос событий")
    calA.add("one", "Созвон с куратором", at(d(1), 10), at(d(1), 11, 30), description="Ссылка:<br>https://meet.x/<b>abc</b> &amp; план")
    almaty_start = datetime(d(2).year, d(2).month, d(2).day, 15, 0, tzinfo=ALMATY)
    calA.add("kz", "Звонок маме", {"dateTime": almaty_start.isoformat()},
             {"dateTime": (almaty_start + timedelta(hours=1)).isoformat()})
    calA.add("allday", "День рождения Арсена", {"date": d(3).isoformat()}, {"date": d(5).isoformat()})
    calA.add("rec", "Лекция по праву", at(d(1), 9), at(d(1), 10, 30), recurrence=["RRULE:FREQ=WEEKLY"])
    for k in range(10):
        day = d(1 + 7 * k)
        calA.add(f"rec_{day:%Y%m%d}", "Лекция по праву", at(day, 9), at(day, 10, 30), recurringEventId="rec")
    calA.add("far", "Экзамен", at(d(60), 9), at(d(60), 12))
    calA.add("past", "Было вчера", at(d(-1), 9), at(d(-1), 10))
    calA.add("declined", "Чужая встреча", at(d(2), 9), at(d(2), 10), attendees=[{"self": True, "responseStatus": "declined"}])
    calA.add("office", "Офис", {"date": d(1).isoformat()}, {"date": d(2).isoformat()}, eventType="workingLocation")
    calA.add("ours", "Наша", at(d(4), 9), at(d(4), 10), extendedProperties={"private": {"julyera": "1"}})
    calA.add("oldours", "📚 🟡 Старая наша", at(d(4), 11), at(d(4), 12))
    calA.add("gone", "Отменили до переноса", at(d(4), 13), at(d(4), 14))
    calA.edit("gone", status="cancelled")
    calB.add("b1", "Событие B", at(d(1), 8), at(d(1), 9))

    calendar_sync.PAGE_SIZE = 4  # заодно проверяем чтение по страницам
    r = client.post("/internal/reminders/run", headers={"X-Reminder-Secret": os.environ["REMINDERS_SECRET"]})
    check("рассылка с синхронизацией отвечает 200", r.status_code == 200, r.text[:200])
    ids = {t["google_event_id"] for t in tasks_of(A)}
    expected = {"one", "kz", "allday", "far"} | {f"rec_{d(1 + 7 * k):%Y%m%d}" for k in range(5)}
    check("перенесены будущие события и повторения на 30 дней (9 задач)", ids == expected, str(sorted(ids)))
    check("прошлые, отклонённые, «где я работаю», отменённые и созданные JulYera — не переносятся",
          not ids & {"past", "declined", "office", "ours", "oldours", "gone", "rec"})
    one = by_event(A, "one")
    check("время и заметка: 10:00, HTML описания убран",
          naive(one["deadline"]) == datetime(d(1).year, d(1).month, d(1).day, 10)
          and one["note"] == "Ссылка:\nhttps://meet.x/abc & план" and one["gcal_imported"] is True)
    kz = by_event(A, "kz")
    check("время из другого пояса пересчитано в пояс пользователя",
          naive(kz["deadline"]) == almaty_start.astimezone(ROME).replace(tzinfo=None))
    allday = by_event(A, "allday")
    check("событие на весь день → 23:59 этого дня, флаг gcal_all_day",
          naive(allday["deadline"]) == datetime(d(3).year, d(3).month, d(3).day, 23, 59) and allday["gcal_all_day"] is True)
    check("категории: от модели, неизвестная → первая категория",
          one["category"] == "Работа" and by_event(A, f"rec_{d(1):%Y%m%d}")["category"] == "Учёба"
          and allday["category"] == "Работа")
    check("модель получила только названия", all(isinstance(t, str) for batch in titles_seen for t in batch))
    check("задачи B — только из календаря B", {t["google_event_id"] for t in tasks_of(B)} == {"b1"}
          and not tasks_of(C))
    to_a = [t for c, t in sent if c == A["telegram_user_id"]]
    check("одно сообщение «перенёс 9 событий»", len(to_a) == 1 and "9" in to_a[0], str(to_a))

    sent.clear()
    before = len(db.tables["tasks"])
    calendar_sync.run_all()
    check("повторный проход ничего не дублирует и не пишет", len(db.tables["tasks"]) == before and not sent)

    # --------------------------------------------------------------- правки в Google → задачи
    section("Правки в Google → задачи")
    calA.edit("one", summary="Созвон с куратором (перенесли)", start=at(d(2), 16), end=at(d(2), 17, 30))
    calendar_sync.run_all()
    one = by_event(A, "one")
    check("новое название и время из календаря",
          one["task"] == "Созвон с куратором (перенесли)" and naive(one["deadline"]).hour == 16
          and naive(one["deadline"]).date() == d(2))
    calA.edit("allday", start=at(d(3), 12), end=at(d(3), 13))
    calendar_sync.run_all()
    allday = by_event(A, "allday")
    check("событие на весь день стало со временем — флаг снят", allday["gcal_all_day"] is False
          and naive(allday["deadline"]).hour == 12)
    calA.add("new", "Новое событие", at(d(5), 18), at(d(5), 19))
    calendar_sync.run_all()
    check("новое событие появилось задачей при следующем проходе", by_event(A, "new") is not None)
    later = calendar_sync.datetime.now(timezone.utc) + timedelta(days=7)
    calendar_sync.run_all(now_utc=later)
    check("окно сдвинулось — подтянулось следующее повторение",
          by_event(A, f"rec_{d(1 + 7 * 5):%Y%m%d}") is not None)

    # --------------------------------------------------------------- правки в JulYera → событие
    section("Правки в JulYera → событие")
    calA.calls.clear()
    r = client.patch(f"/tasks/{one['id']}", headers=hA, json={"task": "Созвон (итог)", "deadline": f"{d(3)}T09:15:00"})
    patch = next((c for c in calA.calls if c[0] == "patch"), None)
    ev = calA.events["one"]
    check("импортированное событие правится patch-ем, без писем гостям",
          r.status_code == 200 and patch and patch[2] == "none" and not any(c[0] == "update" for c in calA.calls))
    check("название без эмодзи, длительность события сохранена (1,5 часа)",
          ev["summary"] == "Созвон (итог)" and _ts(ev["end"]) - _ts(ev["start"]) == timedelta(minutes=90)
          and _ts(ev["start"]).astimezone(ROME).hour == 9)
    after_patch = len(calA.calls)
    calendar_sync.run_all()
    check("наша же правка не возвращается обратно как «правка из Google»",
          by_event(A, "one")["task"] == "Созвон (итог)" and not any(c[0] == "patch" for c in calA.calls[after_patch:]))

    birthday = by_event(A, f"rec_{d(8):%Y%m%d}")
    calA.add("ad2", "Весь день", {"date": d(6).isoformat()}, {"date": d(7).isoformat()})
    calendar_sync.run_all()
    ad2 = by_event(A, "ad2")
    client.patch(f"/tasks/{ad2['id']}", headers=hA, json={"deadline": f"{d(9)}T23:59:00"})
    check("перенос задачи на весь день → событие остаётся на весь день",
          calA.events["ad2"]["start"] == {"date": d(9).isoformat()} and calA.events["ad2"]["end"] == {"date": d(10).isoformat()})
    client.patch(f"/tasks/{ad2['id']}", headers=hA, json={"deadline": f"{d(9)}T14:00:00"})
    check("задаче поставили время → событие со временем",
          "dateTime" in calA.events["ad2"]["start"] and "date" not in calA.events["ad2"]["start"])

    calA.calls.clear()
    r = client.patch(f"/tasks/{birthday['id']}", headers=hA, json={"status": "Done"})
    check("закрыли импортированную задачу — событие остаётся в календаре",
          r.status_code == 200 and calA.events[birthday["google_event_id"]]["status"] == "confirmed"
          and not any(c[0] == "delete" for c in calA.calls) and by_event(A, birthday["google_event_id"]))
    count = len(tasks_of(A))
    calendar_sync.run_all()
    check("…и не переносится заново", len(tasks_of(A)) == count)

    far = by_event(A, "far")
    calA.calls.clear()
    r = client.delete(f"/tasks/{far['id']}", headers=hA)
    dele = next((c for c in calA.calls if c[0] == "delete"), None)
    check("удалили задачу — событие удалено без писем гостям", r.status_code in (200, 204) and dele == ("delete", "far", "none"))

    # --------------------------------------------------------------- удаление в Google
    section("Удаление в Google")
    calA.edit("kz", status="cancelled")
    calA.edit(birthday["google_event_id"], status="cancelled")
    calendar_sync.run_all()
    check("событие удалили в календаре — незакрытая задача удалена", by_event(A, "kz") is None
          and not any(t["task"] == "Звонок маме" for t in tasks_of(A)))
    archived = next(t for t in tasks_of(A) if t["id"] == birthday["id"])
    check("закрытая задача остаётся в архиве без ссылки", archived["status"] == "Done" and archived["google_event_id"] is None)

    del calA.events["new"]  # пропало совсем (404)
    calendar_sync.run_all()
    check("событие пропало (404) — задача удалена после проверки", by_event(A, "new") is None)

    # сбой списка (не все страницы) — ничего не удаляем по отсутствию
    calendar_sync.MAX_PAGES = 1
    calendar_sync.run_all()
    check("список прочитан не целиком — по отсутствию ничего не удаляется",
          by_event(A, "one") is not None and len(tasks_of(A)) > 3)
    calendar_sync.MAX_PAGES = 8

    # --------------------------------------------------------------- задачи, созданные в JulYera
    section("Задачи JulYera и их события")
    r = client.post("/tasks", headers=hA, json={"task": "Сдать отчёт", "category": "Работа", "priority": "High",
                                                "deadline": f"{d(2)}T18:00:00"})
    mine = r.json()
    ev_id = mine.get("google_event_id")
    check("событие создано с меткой JulYera", ev_id in calA.events
          and calA.events[ev_id]["extendedProperties"] == {"private": {"julyera": "1"}})
    count = len(tasks_of(A))
    calendar_sync.run_all()
    check("своё событие не становится второй задачей", len(tasks_of(A)) == count)
    calA.edit(ev_id, start=at(d(4), 11), end=at(d(4), 11, 30), summary="💼 🔴 Сдать отчёт v2")
    calendar_sync.run_all()
    mine = next(t for t in tasks_of(A) if t["id"] == mine["id"])
    check("передвинули наше событие в Google — задача переехала, эмодзи из названия убраны",
          naive(mine["deadline"]) == datetime(d(4).year, d(4).month, d(4).day, 11) and mine["task"] == "Сдать отчёт v2")
    old = {"id": "legacy", "user_id": A["id"], "task": "Старая", "category": "Работа", "priority": "Mid",
           "status": "To Do", "deadline": f"{d(3)}T08:00:00", "google_event_id": "legacyev"}
    db.tables["tasks"].append(old)
    calA.add("legacyev", "🧍 🟡 Старая (переименовал)", at(d(3), 8), at(d(3), 8, 30))
    calendar_sync.run_all()
    legacy = next(t for t in tasks_of(A) if t["id"] == "legacy")
    check("задача до этой версии (без отметки) — сначала только запоминаем, ничего не меняем",
          legacy["task"] == "Старая" and legacy.get("gcal_updated"))
    r = client.patch(f"/tasks/{mine['id']}", headers=hA, json={"status": "Done"})
    check("закрыли задачу JulYera — её событие удаляется, как раньше", calA.events[ev_id]["status"] == "cancelled")

    # --------------------------------------------------------------- выключатель
    section("Выключатель «Переносить события в задачи»")
    check("по умолчанию включён", client.get("/me", headers=hA).json().get("calendar_import") is True)
    r = client.patch("/me", headers=hA, json={"calendar_import": False})
    check("выключение сохраняется, валюта не трогается",
          r.status_code == 200 and r.json()["calendar_import"] is False and r.json()["default_currency"])
    calA.add("whileoff", "Пока выключено", at(d(6), 9), at(d(6), 10))
    calA.edit("one", summary="Переименовал, пока выключено")
    calA.calls.clear()
    calendar_sync.run_all()
    check("выключено — календарь не читается, новых задач и правок нет",
          not calA.calls and by_event(A, "whileoff") is None and by_event(A, "one")["task"] == "Созвон (итог)")
    check("у B (включено) всё работает как раньше", client.get("/me", headers=hB).json()["calendar_import"] is True)
    r = client.patch("/me", headers=hA, json={"calendar_import": True})
    check("включили — сразу подтянулось накопившееся",
          r.json()["calendar_import"] is True and by_event(A, "whileoff") is not None
          and by_event(A, "one")["task"] == "Переименовал, пока выключено")
    check("чужой выключатель не меняется", client.get("/me", headers=hB).json()["calendar_import"] is True)
    check("смена валюты по-прежнему работает",
          client.patch("/me", headers=hB, json={"default_currency": "KZT"}).json()["default_currency"] == "KZT")

    # --------------------------------------------------------------- выбор календарей
    section("Выбор календарей")
    accA.list_forbidden = True
    r = client.get("/google/calendars", headers=hA)
    check("старое подключение (нет права на список) → «переподключите», без пометки «отключился»",
          r.status_code == 200 and r.json()["needs_upgrade"] is True
          and not next(c for c in db.tables["google_credentials"] if c["user_id"] == A["id"])["needs_reconnect"])
    accA.list_forbidden = False
    accA.list_items = [
        {"id": "a@gmail.com", "summary": "a@gmail.com", "primary": True, "accessRole": "owner", "backgroundColor": "#9fe1e7"},
        {"id": "study@group", "summary": "Учёба", "accessRole": "owner", "backgroundColor": "#f83a22"},
        {"id": "hol@group", "summary": "Праздники", "accessRole": "reader"},
        {"id": "hidden@group", "summary": "Скрытый", "accessRole": "owner", "hidden": True},
    ]
    accA.cals["study@group"], accA.cals["hol@group"] = FakeCalendar(), FakeCalendar()
    study, hol = accA.cals["study@group"], accA.cals["hol@group"]
    study.add("st1", "Лекция по экономике", at(d(2), 11), at(d(2), 12, 30))
    for k in range(3):
        day = d(3 + 7 * k)
        study.add(f"strec_{day:%Y%m%d}", "Семинар", at(day, 15), at(day, 16), recurringEventId="strec")
    hol.add("h1", "Праздник", {"date": d(10).isoformat()}, {"date": d(11).isoformat()})
    r = client.get("/google/calendars", headers=hA).json()
    names = [(c["id"], c["name"], c["selected"], c["read_only"]) for c in r["calendars"]]
    check("список: основной первым и выбран, скрытые не показаны, «только чтение» помечен",
          names == [("primary", "Основной", True, False), ("hol@group", "Праздники", False, True),
                    ("study@group", "Учёба", False, False)], str(names))
    check("чужой список не виден: у B свои календари",
          client.get("/google/calendars", headers=hB).status_code in (200, 409) and not cals["tok-B"].list_items)
    check("неизвестный календарь → 422", client.put("/google/calendars", headers=hA, json={"ids": ["evil@group"]}).status_code == 422)
    check("пустой выбор → 422", client.put("/google/calendars", headers=hA, json={"ids": []}).status_code == 422)
    before = len(tasks_of(A))
    r = client.put("/google/calendars", headers=hA, json={"ids": ["primary", "study@group", "hol@group"]})
    check("выбор сохранён, события перенесены сразу (1 + 3 + 1)", r.status_code == 200 and r.json()["created"] == 5
          and len(tasks_of(A)) == before + 5, r.text[:200])
    st1 = by_event(A, "st1")
    check("у задачи записан её календарь", st1["gcal_calendar_id"] == "study@group"
          and by_event(A, "h1")["gcal_calendar_id"] == "hol@group")
    check("основной по-прежнему синхронизируется", by_event(A, "whileoff") is not None)
    check("в списке теперь отмечены три", sum(c["selected"] for c in client.get("/google/calendars", headers=hA).json()["calendars"]) == 3)
    study.calls.clear()
    client.patch(f"/tasks/{st1['id']}", headers=hA, json={"deadline": f"{d(2)}T13:00:00"})
    check("правка задачи уходит в её календарь, а не в основной",
          any(c[0] == "patch" for c in study.calls) and _ts(study.events["st1"]["start"]).astimezone(ROME).hour == 13)
    h1 = by_event(A, "h1")
    r = client.patch(f"/tasks/{h1['id']}", headers=hA, json={"task": "Праздник!"})
    check("календарь только для чтения: задача меняется, «переподключите» не появляется",
          r.status_code == 200 and not next(c for c in db.tables["google_credentials"] if c["user_id"] == A["id"])["needs_reconnect"])
    study.edit("st1", summary="Лекция перенесена")
    calendar_sync.run_all()
    check("правка в выбранном календаре → задача", by_event(A, "st1")["task"] == "Лекция перенесена")
    study.edit(f"strec_{d(3):%Y%m%d}", status="cancelled")
    calendar_sync.run_all()
    check("удалили повторение в выбранном календаре → задача удалена", by_event(A, f"strec_{d(3):%Y%m%d}") is None)
    st1 = by_event(A, "st1")
    study.calls.clear()
    client.delete(f"/tasks/{st1['id']}", headers=hA)
    check("удалили задачу → событие удалено в её календаре", ("delete", "st1", "none") in study.calls)
    client.put("/google/calendars", headers=hA, json={"ids": ["primary"]})
    del study.events[f"strec_{d(10):%Y%m%d}"]
    study.calls.clear()
    calendar_sync.run_all()
    check("календарь сняли с выбора — его задачи остаются и не проверяются",
          by_event(A, f"strec_{d(10):%Y%m%d}") is not None and not study.calls)
    accA.cals.pop("hol@group")
    client.put("/google/calendars", headers=hA, json={"ids": ["primary", "hol@group"]})
    calendar_sync.run_all()
    check("выбранный календарь пропал (404) — основной всё равно синхронизируется, флаг не ставится",
          not next(c for c in db.tables["google_credentials"] if c["user_id"] == A["id"])["needs_reconnect"])

    # --------------------------------------------------------------- переподключение и подключение
    section("Подключение и сбои")
    calB.calls.clear()
    next(r for r in db.tables["google_credentials"] if r["user_id"] == B["id"])["needs_reconnect"] = True
    calendar_sync.run_all()
    check("календарь B требует переподключения — его не читаем", not calB.calls)
    check("у C нет календаря — задач не появилось", not tasks_of(C))

    class Boom:
        def events(self):
            return SimpleNamespace(list=lambda **_: (_ for _ in ()).throw(RuntimeError("сеть")))
    cals["tok-A"] = Boom()
    count = len(tasks_of(A))
    calendar_sync.run_all()
    check("сбой Google не ломает проход и ничего не удаляет", len(tasks_of(A)) == count)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
