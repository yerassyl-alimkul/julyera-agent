"""
Проверки трекера расходов и доходов (критерии приёмки задачи №3).

Режимы:
    python scripts/money_check.py
        Локально: настоящий main.py и сервисы, база в памяти (scripts/fake_supabase.py), без сети.
        HTTP-изоляция, валидация, точность, категории, итоги, повторяющиеся платежи, бот (разбор подменён).

    python scripts/money_check.py --model
        То же плюс разбор фраз настоящей моделью Groq (нужен GROQ_API_KEY в окружении или .env).

    python scripts/money_check.py --base-url https://julyera-agent.onrender.com
        Против работающего сервиса: HTTP-проверки на двух временных пользователях в настоящей базе,
        в конце их данные и сами пользователи удаляются. Бот и повторяющиеся платежи не трогаются.

Все данные вымышленные. Ключи и токены не выводятся. Код выхода 1, если хоть одна проверка не прошла.
"""
import argparse
import asyncio
import os
import re
import secrets
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import isolation_check as ic  # общие помощники: check, section, заголовки входа, фейковые апдейты Telegram

check, section = ic.check, ic.section


# ---------------------------------------------------------------- HTTP (оба режима)

def http_checks(client, token, A, B, db_insert):
    hA = ic.init_data_headers(A["telegram_user_id"], token, "Тест A")
    hB = ic.init_data_headers(B["telegram_user_id"], token, "Тест B")

    def cats(h, kind):
        return client.get("/money/categories", headers=h, params={"kind": kind}).json()

    section("Стартовые категории")
    exp_a, inc_a = cats(hA, "expense"), cats(hA, "income")
    check("у A 8 категорий расходов и 2 доходов", len(exp_a) == 8 and len(inc_a) == 2,
          f"{len(exp_a)} и {len(inc_a)}")
    food_a = next(c for c in exp_a if c["name"] == "Еда")
    other_a = next(c for c in exp_a if c["name"] == "Прочее")
    salary_a = next(c for c in inc_a if c["name"] == "Зарплата")
    food_b = next(c for c in cats(hB, "expense") if c["name"] == "Еда")
    check("категории A не видны B", not {c["id"] for c in exp_a} & {c["id"] for c in cats(hB, "expense")})

    section("Операции и изоляция")
    r = client.post("/money/transactions", headers=hA, json={
        "kind": "expense", "amount": "3,5", "currency": "EUR", "category_id": food_a["id"],
        "spent_on": "2026-09-10", "note": "  Кофе  "})
    tx = r.json()
    check("сумма «3,5» принимается как 3.50, заметка без пробелов по краям",
          r.status_code == 201 and tx.get("amount") == "3.50" and tx.get("note") == "Кофе", str(r.status_code))
    rec = client.post("/money/recurring", headers=hA, json={
        "kind": "expense", "amount": "650", "currency": "EUR", "category_id": other_a["id"],
        "day_of_month": 1, "note": "Аренда"}).json()
    check("A создаёт повторяющийся платёж", bool(rec.get("id")) and rec.get("amount") == "650.00")
    listed_b = client.get("/money/transactions", headers=hB, params={"from": "2026-09-01", "to": "2026-09-30"}).json()
    check("операции A не видны B в списке", all(t["id"] != tx["id"] for t in listed_b))
    check("платежи A не видны B в списке", all(x["id"] != rec["id"] for x in client.get("/money/recurring", headers=hB).json()))
    for method in ("get", "patch", "delete"):
        kw = {"json": {"amount": "1"}} if method == "patch" else {}
        r1 = getattr(client, method)(f"/money/transactions/{tx['id']}", headers=hB, **kw)
        r2 = (getattr(client, method)(f"/money/recurring/{rec['id']}", headers=hB, **kw)
              if method != "get" else SimpleNamespace(status_code=404))
        check(f"{method.upper()} чужой операции и платежа → 404", r1.status_code == 404 and r2.status_code == 404,
              f"{r1.status_code}/{r2.status_code}")
    check("PATCH и DELETE чужой категории → 404",
          client.patch(f"/money/categories/{food_a['id']}", headers=hB, json={"name": "взлом"}).status_code == 404
          and client.delete(f"/money/categories/{food_a['id']}", headers=hB).status_code == 404)
    check("после попыток B операция A не изменилась",
          client.get(f"/money/transactions/{tx['id']}", headers=hA).json().get("amount") == "3.50")
    sb = client.get("/money/summary", headers=hB, params={"month": "2026-09"}).json()
    check("/money/summary у B не видит операций A", sb.get("currencies") == [])
    check("/money/trend у B не видит операций A",
          client.get("/money/trend", headers=hB, params={"months": 12}).json().get("currencies") == [])

    section("Чужие и неподходящие ссылки")
    r = client.post("/money/transactions", headers=hA, json={
        "kind": "expense", "amount": "1", "currency": "EUR", "category_id": food_b["id"]})
    check("операция с чужой категорией → 422", r.status_code == 422, str(r.status_code))
    r = client.post("/money/transactions", headers=hA, json={
        "kind": "expense", "amount": "1", "currency": "EUR", "category_id": salary_a["id"]})
    check("расход с категорией дохода → 422", r.status_code == 422, str(r.status_code))
    r = client.patch(f"/money/transactions/{tx['id']}", headers=hA, json={"category_id": food_b["id"]})
    check("смена категории на чужую → 422", r.status_code == 422, str(r.status_code))
    r = client.patch(f"/money/transactions/{tx['id']}", headers=hA, json={"category_id": other_a["id"]})
    check("смена категории на свою того же типа работает",
          r.status_code == 200 and r.json().get("category_id") == other_a["id"])
    client.patch(f"/money/transactions/{tx['id']}", headers=hA, json={"category_id": food_a["id"]})
    r = client.delete(f"/money/categories/{food_a['id']}", headers=hA, params={"move_to": food_b["id"]})
    check("удаление с чужим move_to → 404", r.status_code == 404, str(r.status_code))
    r = client.delete(f"/money/categories/{food_a['id']}", headers=hA, params={"move_to": salary_a["id"]})
    check("удаление с move_to другого типа → 422", r.status_code == 422, str(r.status_code))

    section("Проверка значений")
    base = {"kind": "expense", "currency": "EUR", "category_id": food_a["id"], "spent_on": "2026-09-11"}
    for amount, why in (("0", "ноль"), ("-5", "отрицательная"), ("1.005", "три знака"),
                        ("1000000000000", "10^12"), ("abc", "не число")):
        r = client.post("/money/transactions", headers=hA, json={**base, "amount": amount})
        check(f"сумма: {why} → 422", r.status_code == 422, str(r.status_code))
    r = client.post("/money/transactions", headers=hA, json={**base, "amount": "1", "currency": "GBP"})
    check("валюта GBP (её нет) → 422", r.status_code == 422)
    for day, why in (("2026-13-01", "13-й месяц"), ("10.09.2026", "не ГГГГ-ММ-ДД"), ("2040-01-01", "дальше 5 лет")):
        r = client.post("/money/transactions", headers=hA, json={**base, "amount": "1", "spent_on": day})
        check(f"дата: {why} → 422", r.status_code == 422, str(r.status_code))
    r = client.post("/money/transactions", headers=hA, json={**base, "amount": "1", "note": "x" * 201})
    check("заметка длиннее 200 символов → 422", r.status_code == 422)
    msg = client.post("/money/transactions", headers=hA, json={**base, "amount": "0"}).json()
    check("текст ошибки понятный, по-русски", "больше нуля" in str(msg))
    check("limit больше 200 → 422", client.get("/money/transactions", headers=hA, params={"limit": 201}).status_code == 422)
    check("trend months=25 → 422", client.get("/money/trend", headers=hA, params={"months": 25}).status_code == 422)

    section("Точность сумм")
    for amount in ("0.10", "0.20"):
        client.post("/money/transactions", headers=hB, json={
            "kind": "expense", "amount": amount, "currency": "EUR", "category_id": food_b["id"], "spent_on": "2026-06-05"})
    s = client.get("/money/summary", headers=hB, params={"month": "2026-06"}).json()
    check("0.10 + 0.20 = ровно 0.30", s["currencies"][0]["expense"] == "0.30", str(s["currencies"]))
    rows, expected = [], Decimal(0)
    for i in range(1050):
        amount = Decimal(i % 997 + 1) / 100 + 7
        expected += amount
        rows.append({"user_id": A["id"], "kind": "expense", "amount": str(amount), "currency": "KZT",
                     "category_id": food_a["id"], "spent_on": f"2026-07-{i % 28 + 1:02d}", "source": "app"})
    db_insert("money_transactions", rows)
    s = client.get("/money/summary", headers=hA, params={"month": "2026-07"}).json()
    kzt = next((c for c in s["currencies"] if c["currency"] == "KZT"), {})
    check("1050 операций за месяц: сумма точная, без обрезки на 1000 строках",
          kzt.get("expense") == str(expected.quantize(Decimal("0.01"))), f"{kzt.get('expense')} ≠ {expected}")

    section("Итоги и динамика")
    for kind, amount, cur, cat, day in (("expense", "40", "EUR", food_a, "2026-08-03"),
                                        ("expense", "10", "EUR", other_a, "2026-08-04"),
                                        ("income", "1800", "EUR", salary_a, "2026-08-01"),
                                        ("expense", "1500", "KZT", food_a, "2026-08-02"),
                                        ("expense", "20", "EUR", food_a, "2026-07-02")):
        client.post("/money/transactions", headers=hA, json={
            "kind": kind, "amount": amount, "currency": cur, "category_id": cat["id"], "spent_on": day})
    s = client.get("/money/summary", headers=hA, params={"month": "2026-08"}).json()
    eur = next((c for c in s["currencies"] if c["currency"] == "EUR"), {})
    kzt = next((c for c in s["currencies"] if c["currency"] == "KZT"), {})
    check("итоги EUR: расходы 50.00, доходы 1800.00, баланс 1750.00",
          (eur.get("expense"), eur.get("income"), eur.get("balance")) == ("50.00", "1800.00", "1750.00"), str(eur))
    check("валюты не смешиваются: KZT отдельно (1500.00)", kzt.get("expense") == "1500.00" and kzt.get("income") == "0.00")
    check("расходы по категориям по убыванию с долей",
          [(c["name"], c["amount"], c["share"]) for c in eur.get("by_category", [])] == [("Еда", "40.00", 80), ("Прочее", "10.00", 20)],
          str(eur.get("by_category")))
    check("предыдущий месяц для сравнения (июль: 20.00 EUR)", eur.get("previous", {}).get("expense") == "20.00")
    main = s.get("main") or {}
    rate = Decimal((s.get("rate") or {}).get("kzt", "0") or "0")
    kzt_in_eur = (Decimal("1500") / rate).quantize(Decimal("0.01")) if rate else None
    check("курс в ответе: сколько тенге за 1 €", rate > 0 and s["rate"]["currency"] == "EUR", str(s.get("rate")))
    check("основная валюта: общий итог расходов = 50.00 € + 1500 ₸ по курсу",
          main.get("currency") == "EUR" and kzt_in_eur is not None
          and main.get("expense") == str((Decimal("50.00") + kzt_in_eur)), f"{main.get('expense')} / {kzt_in_eur}")
    check("строки по валютам: исходная сумма и пересчёт",
          main.get("parts", {}).get("expense") == [{"currency": "EUR", "amount": "50.00", "converted": "50.00"},
                                                   {"currency": "KZT", "amount": "1500.00", "converted": str(kzt_in_eur)}],
          str(main.get("parts")))
    check("категории в основной валюте объединяют обе валюты, «≈» только где был пересчёт",
          [(c["name"], c["amount"], c["approx"]) for c in main.get("by_category", [])] ==
          [("Еда", str(Decimal("40.00") + kzt_in_eur), True), ("Прочее", "10.00", False)], str(main.get("by_category")))
    rows_kzt = client.get("/money/transactions", headers=hA, params={"from": "2026-08-01", "to": "2026-08-31", "currency": "KZT"}).json()
    check("операция в тенге показывает пересчёт в основную валюту",
          rows_kzt and rows_kzt[0].get("converted") == {"amount": str(kzt_in_eur), "currency": "EUR"}, str(rows_kzt[:1]))
    rows_eur = client.get("/money/transactions", headers=hA, params={"from": "2026-08-01", "to": "2026-08-31", "currency": "EUR"}).json()
    check("операция в основной валюте без пересчёта", rows_eur and "converted" not in rows_eur[0])
    t = client.get("/money/trend", headers=hA, params={"months": 12}).json()
    check("динамика в основной валюте", (t.get("main") or {}).get("currency") == "EUR" and len(t["main"]["points"]) == 12)
    check("разница расходов с прошлым месяцем считается на сервере (+30.00)",
          eur.get("previous", {}).get("expense_change") == "30.00")
    check("суммы по дням для заголовков списка",
          [(d["day"], d["expense"], d["income"]) for d in eur.get("days", [])] ==
          [("2026-08-04", "10.00", "0.00"), ("2026-08-03", "40.00", "0.00"), ("2026-08-01", "0.00", "1800.00")])
    s_sep = client.get("/money/summary", headers=hA, params={"month": "2026-05"}).json()
    check("валюта без операций в месяце не появляется", s_sep.get("currencies") == [])
    check("месяц в неверном виде → 422", client.get("/money/summary", headers=hA, params={"month": "2026-8"}).status_code == 422)
    t = client.get("/money/trend", headers=hA, params={"months": 6}).json()
    check("динамика: 6 месяцев, отдельно по валютам", len(t.get("months", [])) == 6 and
          {c["currency"] for c in t.get("currencies", [])} <= {"EUR", "KZT"})
    page = client.get("/money/transactions", headers=hA, params={"from": "2026-07-01", "to": "2026-07-31", "limit": 200}).json()
    page2 = client.get("/money/transactions", headers=hA,
                       params={"from": "2026-07-01", "to": "2026-07-31", "limit": 200, "offset": 200}).json()
    check("список по 200 строк, новые сверху, offset даёт следующую страницу",
          len(page) == 200 and len(page2) == 200 and page[0]["spent_on"] >= page[-1]["spent_on"]
          and not {x["id"] for x in page} & {x["id"] for x in page2})

    section("Категории денег")
    r = client.post("/money/categories", headers=hA, json={"kind": "expense", "name": "  еда ", "color": "#A9C89D"})
    check("дубль названия без учёта регистра → 409", r.status_code == 409, str(r.status_code))
    r = client.post("/money/categories", headers=hA, json={"kind": "income", "name": "Еда", "color": "#A9C89D"})
    check("то же название у другого типа можно", r.status_code == 201, str(r.status_code))
    check("цвет вне палитры → 422", client.post("/money/categories", headers=hA,
          json={"kind": "expense", "name": "Цвет", "color": "#123456"}).status_code == 422)
    r = client.delete(f"/money/categories/{food_a['id']}", headers=hA)
    check("удаление категории с операциями без move_to → 409", r.status_code == 409, str(r.status_code))
    r = client.delete(f"/money/categories/{other_a['id']}", headers=hA, params={"move_to": food_a["id"]})
    moved_rec = next(x for x in client.get("/money/recurring", headers=hA).json() if x["id"] == rec["id"])
    s = client.get("/money/summary", headers=hA, params={"month": "2026-08"}).json()
    eur = next(c for c in s["currencies"] if c["currency"] == "EUR")
    check("удаление с move_to переносит операции и платежи",
          r.status_code == 204 and moved_rec["category_id"] == food_a["id"]
          and [c["name"] for c in eur["by_category"]] == ["Еда"], str(r.status_code))
    r = client.patch(f"/money/categories/{food_a['id']}", headers=hA, json={"name": "Продукты", "color": "#F2B8A2"})
    check("переименование и перекраска категории", r.status_code == 200 and r.json()["name"] == "Продукты"
          and r.json()["color"] == "#F2B8A2")
    ids = [c["id"] for c in cats(hA, "expense")][::-1]
    client.put("/money/categories/order", headers=hA, json={"kind": "expense", "ids": ids})
    check("порядок категорий меняется", [c["id"] for c in cats(hA, "expense")] == ids)
    check("порядок с чужим id → 422", client.put("/money/categories/order", headers=hB,
          json={"kind": "expense", "ids": [c["id"] for c in cats(hB, "expense")] + [food_a["id"]]}).status_code == 422)
    inc = cats(hA, "income")
    for c in inc[1:]:
        client.delete(f"/money/categories/{c['id']}", headers=hA, params={"move_to": inc[0]["id"]})
    left = cats(hA, "income")
    check("последнюю категорию типа удалить нельзя (409)", len(left) == 1 and
          client.delete(f"/money/categories/{left[0]['id']}", headers=hA).status_code == 409)

    section("Валюта по умолчанию и /me")
    r = client.patch("/me", headers=hA, json={"default_currency": "KZT"})
    check("PATCH /me меняет валюту по умолчанию", r.status_code == 200 and r.json().get("default_currency") == "KZT")
    check("PATCH /me с GBP → 422", client.patch("/me", headers=hA, json={"default_currency": "GBP"}).status_code == 422)
    check("у B валюта не изменилась", client.get("/me", headers=hB).json().get("default_currency") == "EUR")
    check("операция без входа → 401", client.get("/money/summary").status_code == 401)
    return {"food_a": food_a, "rec": rec}


def wallet_checks(client, token, B):
    """Повтор из формы траты (переключатель «Повторять»), частоты, правка и остановка, доходы по категориям."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    section("Кошелёк: повтор из формы траты")
    hB = ic.init_data_headers(B["telegram_user_id"], token, "Тест B")
    today = datetime.now(ZoneInfo("Europe/Rome")).date()
    cats = client.get("/money/categories", headers=hB, params={"kind": "expense"}).json()
    cat = cats[0]["id"]

    def tx(repeat, **extra):
        return client.post("/money/transactions", headers=hB, json={
            "kind": "expense", "amount": "9,99", "currency": "EUR", "category_id": cat,
            "spent_on": today.isoformat(), "note": f"Повтор {repeat}", "repeat": repeat, **extra})

    def rec_of(t):
        return next((r for r in client.get("/money/recurring", headers=hB).json() if r["id"] == t.get("recurring_id")), {})

    r = tx("monthly")
    t_month = r.json()
    rec = rec_of(t_month)
    nm = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    import calendar as _cal
    expected_month = nm.replace(day=min(today.day, _cal.monthrange(nm.year, nm.month)[1]))
    check("«Повторять: каждый месяц» создаёт операцию и правило, следующий раз через месяц",
          r.status_code == 201 and rec.get("frequency") == "monthly" and rec.get("anchor_date") == today.isoformat()
          and rec.get("next_run") == expected_month.isoformat(), f"{r.status_code} {rec}")
    rec_w = rec_of(tx("weekly").json())
    check("каждую неделю: следующий раз через 7 дней", rec_w.get("next_run") == (today + timedelta(days=7)).isoformat())
    rec_b = rec_of(tx("biweekly").json())
    check("каждые 2 недели: через 14 дней", rec_b.get("next_run") == (today + timedelta(days=14)).isoformat())
    rec_y = rec_of(tx("yearly").json())
    check("каждый год: через год", rec_y.get("next_run", "").startswith(str(today.year + 1)))
    check("«каждый день» → 422", tx("daily").status_code == 422)
    check("список повторяющихся по типу", all(x["kind"] == "expense" for x in
          client.get("/money/recurring", headers=hB, params={"kind": "expense"}).json())
          and client.get("/money/recurring", headers=hB, params={"kind": "income"}).json() == [])
    r = client.patch(f"/money/recurring/{rec['id']}", headers=hB, json={"frequency": "weekly"})
    check("смена частоты пересчитывает следующий платёж (не раньше сегодня)",
          r.status_code == 200 and r.json()["frequency"] == "weekly" and r.json()["next_run"] >= today.isoformat())
    r = client.patch(f"/money/recurring/{rec['id']}", headers=hB, json={"next_run": (today - timedelta(days=1)).isoformat()})
    check("следующий платёж вчера → 422", r.status_code == 422)
    tomorrow = today + timedelta(days=1)
    r = client.patch(f"/money/recurring/{rec['id']}", headers=hB, json={"next_run": tomorrow.isoformat()})
    check("дата следующего платежа меняется, число месяца берётся от неё",
          r.status_code == 200 and r.json()["next_run"] == tomorrow.isoformat() and r.json()["day_of_month"] == tomorrow.day)
    r = client.delete(f"/money/recurring/{rec['id']}", headers=hB)
    left = client.get(f"/money/transactions/{t_month['id']}", headers=hB)
    check("выключить «Повторять» = остановить правило; операция остаётся",
          r.status_code == 204 and left.status_code == 200 and left.json()["recurring_id"] is None)
    for x in (rec_w, rec_b, rec_y):
        client.delete(f"/money/recurring/{x['id']}", headers=hB)
    inc = client.get("/money/categories", headers=hB, params={"kind": "income"}).json()[0]
    client.post("/money/transactions", headers=hB, json={"kind": "income", "amount": "100", "currency": "EUR",
                                                         "category_id": inc["id"], "spent_on": "2026-06-10"})
    s = client.get("/money/summary", headers=hB, params={"month": "2026-06"}).json()
    eur = next(c for c in s["currencies"] if c["currency"] == "EUR")
    check("итоги: доходы по категориям и разница доходов с прошлым месяцем",
          eur["income_by_category"] and eur["income_by_category"][0]["name"] == inc["name"]
          and eur["income_by_category"][0]["share"] == 100 and "income_change" in eur["previous"])


def applepay_checks(client, token, A, B, local: bool):
    """Покупки из Apple Pay: разбор суммы, категория (память магазинов, модель), повтор, возвраты, изоляция."""
    import money_service

    section("Apple Pay")
    hA = ic.init_data_headers(A["telegram_user_id"], token, "Тест A")
    hB = ic.init_data_headers(B["telegram_user_id"], token, "Тест B")
    for text, expected in (("€23,40", ("23.40", "EUR")), ("23,40 €", ("23.40", "EUR")), ("EUR 1,234.56", ("1234.56", "EUR")),
                           ("4 000 ₸", ("4000.00", "KZT")), ("₸4.000", ("4000.00", "KZT")), ("KZT 4,000.00", ("4000.00", "KZT")),
                           ("12.5", ("12.50", "EUR"))):
        amount, cur, refund = money_service.parse_applepay_amount(text, "EUR")
        check(f"сумма «{text}» → {expected[0]} {expected[1]}", (str(amount), cur, refund) == (*expected, False))
    check("«-5,00 €» — это возврат", money_service.parse_applepay_amount("-5,00 €", "EUR")[2] is True)

    def pay(h, amount, merchant):
        return client.post("/money/applepay", headers=h, json={"amount": amount, "merchant": merchant, "card": "Visa"})

    r = pay(hA, "€23,40", "ESSELUNGA SpA 123")
    tx = r.json().get("transaction", {})
    names = {c["id"]: c for c in client.get("/money/categories", headers=hA, params={"kind": "expense"}).json()}
    check("покупка записана как расход Apple Pay в своей категории расходов",
          r.status_code == 201 and tx.get("amount") == "23.40" and tx.get("currency") == "EUR"
          and tx.get("source") == "applepay" and tx.get("category_id") in names, f"{r.status_code} {r.text[:120]}")
    r2 = pay(hA, "23,40 €", "ESSELUNGA SpA 123")
    check("та же покупка через минуту не записывается второй раз",
          r2.status_code == 201 and r2.json().get("duplicate") is True and r2.json()["transaction"]["id"] == tx.get("id"))
    other = next(c for c in names.values() if c["id"] != tx.get("category_id"))
    client.patch(f"/money/transactions/{tx['id']}", headers=hA, json={"category_id": other["id"]})
    r3 = pay(hA, "€5,00", "Esselunga S.p.A. 77")
    check("после исправления категории магазин запоминается (другой номер того же магазина)",
          r3.json().get("transaction", {}).get("category_id") == other["id"], r3.text[:120])
    rb = pay(hB, "€5,00", "Esselunga S.p.A. 77")
    check("память магазинов у каждого своя", rb.status_code == 201 and rb.json()["transaction"]["category_id"] != other["id"])
    check("операция Apple Pay другого пользователя → 404", client.get(f"/money/transactions/{tx['id']}", headers=hB).status_code == 404)
    check("возврат не записывается (422)", pay(hA, "-5,00 €", "Esselunga").status_code == 422)
    check("валюта не из пяти (£) → 422 с понятным текстом", "не поддерживается" in pay(hA, "£10.00", "Shop").text)
    check("без входа → 401", client.post("/money/applepay", json={"amount": "€1", "merchant": "X"}).status_code == 401)
    return tx


def limits_checks(client, token, B, alerts):
    """Лимиты: задание, состояние в итогах, предупреждения 80% и 100% по одному разу (alerts — список отправленных
    текстов, только локально; None — не проверять)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    section("Лимиты по категориям")
    hB = ic.init_data_headers(B["telegram_user_id"], token, "Тест B")
    today = datetime.now(ZoneInfo("Europe/Rome")).date().isoformat()
    month = today[:7]
    cat = client.post("/money/categories", headers=hB, json={"kind": "expense", "name": "Лимит тест", "color": "#F2B8A2"}).json()
    inc = client.get("/money/categories", headers=hB, params={"kind": "income"}).json()[0]
    check("лимит у категории доходов → 422",
          client.patch(f"/money/categories/{inc['id']}", headers=hB, json={"monthly_limit": "100"}).status_code == 422)
    r = client.patch(f"/money/categories/{cat['id']}", headers=hB, json={"monthly_limit": "100,00"})
    check("лимит задаётся (запятая допустима) в основной валюте", r.status_code == 200
          and r.json().get("monthly_limit") == "100.00" and r.json().get("limit_currency") == "EUR", r.text[:150])

    def state():
        main = client.get("/money/summary", headers=hB, params={"month": month}).json()["main"]
        return next((c for c in main["by_category"] if c["category_id"] == cat["id"]), {}), main.get("limits")

    c, _ = state()
    check("категория с лимитом видна и без трат", c.get("limit", {}).get("spent") == "0.00" and c["limit"]["state"] == "ok")
    n0 = len(alerts) if alerts is not None else 0

    def spend(amount):
        client.post("/money/transactions", headers=hB, json={"kind": "expense", "amount": amount, "currency": "EUR",
                                                             "category_id": cat["id"], "spent_on": today})
    spend("79")
    check("79% — без предупреждения", alerts is None or len(alerts) == n0)
    spend("2")
    c, _ = state()
    check("81% — состояние «почти» (warn)", c["limit"]["pct"] == 81 and c["limit"]["state"] == "warn", str(c.get("limit")))
    if alerts is not None:
        check("на 80% одно предупреждение в бот", len(alerts) == n0 + 1 and alerts[-1].startswith("⚠️ «Лимит тест»: потрачено 81.00 EUR из 100.00 EUR (81%)"),
              str(alerts[-1:]))
    spend("30")
    c, counts = state()
    check("111% — превышен (over), остаток −11.00", c["limit"]["state"] == "over" and c["limit"]["left"] == "-11.00")
    check("счётчик лимитов в итогах", counts.get("over", 0) >= 1)
    if alerts is not None:
        check("на 100% — «превышен на 11.00 EUR»", len(alerts) == n0 + 2 and "превышен на 11.00 EUR" in alerts[-1], str(alerts[-1:]))
        spend("1")
        check("дальше в этом месяце без повторов", len(alerts) == n0 + 2)
    r = client.patch(f"/money/categories/{cat['id']}", headers=hB, json={"monthly_limit": None})
    check("лимит снимается", r.status_code == 200 and r.json().get("monthly_limit") is None)

    section("Аналитика за период")
    a = client.get("/money/analytics", headers=hB, params={"from": "2026-06-01", "to": "2026-06-30"}).json()
    check("итоги периода в основной валюте", (a.get("expense"), a.get("income"), a.get("days"), a.get("currency"))
          == ("0.30", "100.00", 30, "EUR"), str({k: a.get(k) for k in ("expense", "income", "days", "currency")}))
    check("предыдущий такой же период — май", (a["previous"]["from"], a["previous"]["to"]) == ("2026-05-02", "2026-05-31"))
    check("средний расход в день и места трат", a["avg_per_day"] == "0.01" and a["top_places"] and a["largest"])
    check("конец раньше начала → 422",
          client.get("/money/analytics", headers=hB, params={"from": "2026-06-30", "to": "2026-06-01"}).status_code == 422)
    check("аналитика B не видит трат A", client.get("/money/analytics", headers=hB,
          params={"from": "2026-07-01", "to": "2026-07-31"}).json()["expense"] == "0.00")


# ---------------------------------------------------------------- локально: платежи, бот, распознавание денег

def recurring_checks(db, client, A, B):
    import money_service

    section("Повторяющиеся платежи")
    cat_a = money_service.list_categories(A["id"], "expense")[0]
    cat_b = money_service.list_categories(B["id"], "expense")[0]
    db.tables["money_recurring"] = []

    def make(user, cat, dom, next_run, active=True, note=None):
        row = {"user_id": user["id"], "kind": "expense", "amount": "10", "currency": "EUR", "category_id": cat["id"],
               "day_of_month": dom, "next_run": next_run, "active": active, "note": note}
        return db.table("money_recurring").insert(row).execute().data[0]

    today = {"value": date(2026, 10, 7)}
    rec2 = make(A, cat_a, 5, "2026-08-05", note="Подписка")     # пропущены август, сентябрь, октябрь
    feb = make(A, cat_a, 31, "2028-01-31", note="Февральский")  # 31 января → 29 февраля 2028
    off = make(A, cat_a, 1, "2026-09-01", active=False)
    future_b = make(B, cat_b, 20, "2026-10-20")

    def tx_of(rec):
        return sorted(t["spent_on"] for t in db.tables.get("money_transactions", []) if t.get("recurring_id") == rec["id"])

    weekly = make(A, cat_a, 16, "2026-09-16", note="Еженедельный")
    db.table("money_recurring").update({"frequency": "weekly"}).eq("id", weekly["id"]).execute()
    msgs = money_service.run_recurring(today_of=lambda uid: today["value"])
    check("еженедельный платёж догоняет пропущенные недели",
          tx_of(weekly) == ["2026-09-16", "2026-09-23", "2026-09-30", "2026-10-07"], str(tx_of(weekly)))
    check("пропущенные месяцы догоняются по очереди", tx_of(rec2) == ["2026-08-05", "2026-09-05", "2026-10-05"], str(tx_of(rec2)))
    check("next_run сдвинулся на следующий месяц",
          next(r for r in db.tables["money_recurring"] if r["id"] == rec2["id"])["next_run"] == "2026-11-05")
    check("бот пишет одно сообщение о платеже", any("Добавлен платёж: Подписка · 10.00 EUR" in t for _, t in msgs), str(msgs))
    before = len(db.tables["money_transactions"])
    db.tables["money_recurring"][0]["next_run"] = "2026-10-05"  # как будто прогон упал до сдвига next_run
    money_service.run_recurring(today_of=lambda uid: today["value"])
    check("повторный прогон в тот же день ничего не дублирует", len(db.tables["money_transactions"]) == before)
    check("неактивный платёж ничего не создаёт", tx_of(off) == [])
    check("платёж B в будущем не тронут", tx_of(future_b) == [] and
          next(r for r in db.tables["money_recurring"] if r["id"] == future_b["id"])["next_run"] == "2026-10-20")
    today["value"] = date(2028, 3, 1)
    for r in db.tables["money_recurring"]:
        if r["id"] != feb["id"]:
            r["active"] = False
    money_service.run_recurring(today_of=lambda uid: today["value"], utc_today=today["value"])
    check("день 31 в феврале даёт 29-е (2028 високосный), day_of_month не меняется",
          tx_of(feb) == ["2028-01-31", "2028-02-29"] and
          next(r for r in db.tables["money_recurring"] if r["id"] == feb["id"])["day_of_month"] == 31, str(tx_of(feb)))
    today["value"] = date(2026, 10, 7)
    stuck = make(A, cat_a, 7, "2026-10-07", note="Сломанный")
    ok_rec = make(A, cat_a, 7, "2026-10-07", note="Целый")
    real_table = db.table

    def flaky_table(name):
        q = real_table(name)
        if name == "money_transactions":
            orig = q.insert

            def insert(payload):
                if isinstance(payload, dict) and payload.get("recurring_id") == stuck["id"]:
                    raise RuntimeError("сбой базы")
                return orig(payload)
            q.insert = insert
        return q

    db.table = flaky_table
    try:
        money_service.run_recurring(today_of=lambda uid: today["value"])
    finally:
        db.table = real_table
    check("сбой одного платежа не останавливает остальные", tx_of(ok_rec) == ["2026-10-07"] and tx_of(stuck) == [])

    secret = os.environ["REMINDERS_SECRET"]
    original = money_service.run_recurring
    money_service.run_recurring = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("всё сломалось"))
    try:
        r = client.post("/internal/reminders/run", headers={"X-Reminder-Secret": secret})
    finally:
        money_service.run_recurring = original
    check("сбой платежей не ломает рассылку напоминаний", r.status_code == 200 and "messages" in r.json(), str(r.status_code))


def release_checks(db):
    import release_notes
    import users_service

    section("Заметка о релизе")
    admin = users_service.create_user("Владелец", 1001)
    db.tables["users"][-1]["is_admin"] = True
    admin["is_admin"] = True
    bot = ic.FakeBot()
    first = asyncio.run(release_notes.announce(bot, db, [admin]))
    second = asyncio.run(release_notes.announce(bot, db, [admin]))
    to_admin = [m for m in bot.sent if m.chat_id == 1001]
    check("после деплоя заметка уходит владельцу один раз", first and not second and len(to_admin) == 1
          and to_admin[0].text == release_notes.RELEASE_TEXT)


def detection_checks():
    import money_service

    section("Деньги или задача (без модели)")
    for text, expected in (("кофе 3,5 евро", True), ("вчера такси 12 евро", True), ("5к тенге такси", True),
                           ("€12 обед", True), ("проезд 1500 тг", True), ("купить молоко", False),
                           ("позвонить врачу в 18:00", False), ("встреча в евроклубе 3 числа", False),
                           ("прочитать 5 глав", False), ("12 kzt", True)):
        check(f"«{text}» → {'деньги' if expected else 'задача'}", money_service.looks_like_money(text) == expected)
    check("первое слово «расход» включает денежный режим",
          money_service.money_mode("расход аптека 8") == (True, "expense", "аптека 8"))
    check("первое слово «доход» задаёт тип income", money_service.money_mode("Доход: подарок 50")[1] == "income")


def fake_money_parse(text, categories, today, default_currency):
    """Вместо Groq: «<описание> <число> <валюта>», через запятую. Доход по словам «получил/зарплата»."""
    out = []
    for part in re.split(r",\s+(?=\D)", text):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(евро|тенге)?", part)
        if not m:
            continue
        note = part[:m.start()].strip().capitalize() or None
        income = bool(re.search(r"получил|зарплат", part))
        cur = {"евро": "EUR", "тенге": "KZT"}.get(m.group(2) or "", default_currency)
        out.append({"kind": "income" if income else "expense", "amount": m.group(1).replace(",", "."), "currency": cur,
                    "category": "Зарплата" if income else ("Еда" if "кофе" in part else "Выдуманная"),
                    "note": note, "date": today.isoformat()})
    return out


def bot_checks(db, A, B):
    import groq_client
    import telegram_handlers as h

    run = asyncio.run
    section("Бот: деньги")
    real_parse = groq_client.parse_money_text
    groq_client.parse_money_text = fake_money_parse
    ctx = SimpleNamespace(bot=ic.FakeBot())
    txs = lambda: [t for t in db.tables.get("money_transactions", []) if t["user_id"] == A["id"] and t["source"] == "text"]
    before = len(txs())
    u = ic.tg_update(A["telegram_user_id"], text="кофе 3,5 евро")
    run(h.handle_text(u, ctx))
    created = txs()[before:]
    reply = u.replies[-1]
    buttons = str(reply.markup)
    check("«кофе 3,5 евро» создаёт расход и показывает кнопки",
          len(created) == 1 and Decimal(str(created[0]["amount"])) == Decimal("3.50")
          and "Добавлено: Кофе · 3.50 EUR · Еда" in reply.text
          and all(x in buttons for x in ("Отменить", "Категория", "Это задача")), reply.text)
    tx_id = created[0]["id"]
    check("callback_data не длиннее 64 байт", all(len(b.callback_data.encode()) <= 64
                                                 for row in reply.markup.inline_keyboard for b in row))

    def press(user, data, message_text="Добавлено: Кофе", reply_to=None):
        upd = ic.tg_update(user["telegram_user_id"], callback_data=data)
        upd.callback_query.message = SimpleNamespace(text=message_text, reply_to_message=reply_to)

        async def edit_markup(**_):
            upd.edits.append("[кнопки]")
        upd.callback_query.edit_message_reply_markup = edit_markup
        run(h.money_callback(upd, ctx))
        return upd

    p = press(B, f"m:u:{tx_id}")
    check("«Отменить» чужим пользователем отклоняется без изменений",
          any(t["id"] == tx_id for t in db.tables["money_transactions"]) and p.answers and not p.edits)
    p = press(A, f"m:c:{tx_id}")
    check("«Категория» показывает категории", p.edits == ["[кнопки]"])
    personal = next(c for c in __import__("money_service").list_categories(A["id"], "expense") if c["name"] == "Прочее")
    p = press(A, f"m:s:{tx_id}:{personal['id'][:8]}")
    check("выбор категории кнопкой меняет категорию траты",
          next(t for t in db.tables["money_transactions"] if t["id"] == tx_id)["category_id"] == personal["id"]
          and "Прочее" in p.edits[-1])
    p = press(A, f"m:u:{tx_id}")
    check("«Отменить» удаляет операцию", all(t["id"] != tx_id for t in db.tables["money_transactions"]) and "Отменено" in p.edits[-1])
    p = press(A, f"m:u:{tx_id}")
    check("повторное «Отменить» безопасно", p.answers and not p.edits)

    u = ic.tg_update(A["telegram_user_id"], text="продукты 45 евро, проезд 1500 тенге")
    run(h.handle_text(u, ctx))
    check("несколько трат: одно сообщение со списком и «Отменить всё»",
          "Отменить всё" in str(u.replies[-1].markup) and u.replies[-1].text.count("•") == 2)
    data = u.replies[-1].markup.inline_keyboard[0][0].callback_data
    n = len(txs())
    press(A, data)
    check("«Отменить всё» удаляет обе", len(txs()) == n - 2)

    tasks_before = len([t for t in db.tables["tasks"] if t["user_id"] == A["id"]])
    u = ic.tg_update(A["telegram_user_id"], text="/task оплатить аренду 650 евро до пятницы")
    run(h.task_command(u, ctx))
    check("/task с суммой создаёт задачу, а не операцию",
          len([t for t in db.tables["tasks"] if t["user_id"] == A["id"]]) == tasks_before + 1)

    u = ic.tg_update(A["telegram_user_id"], text="оплатить аренду 650 евро до пятницы")
    run(h.handle_text(u, ctx))
    tx_id = txs()[-1]["id"]
    source_msg = SimpleNamespace(text="оплатить аренду 650 евро до пятницы")
    press(A, f"m:t:{tx_id}", reply_to=source_msg)
    check("«Это задача» удаляет операцию и создаёт задачу",
          all(t["id"] != tx_id for t in db.tables["money_transactions"])
          and any(t["task"] == "оплатить аренду 650 евро до пятницы" for t in db.tables["tasks"]))

    u = ic.tg_update(A["telegram_user_id"], text="купить молоко")
    n = len(txs())
    run(h.handle_text(u, ctx))
    check("«купить молоко» остаётся задачей", len(txs()) == n and any(t["task"] == "купить молоко" for t in db.tables["tasks"]))
    u = ic.tg_update(A["telegram_user_id"], text="/spent кофе")
    run(h.spent_command(u, ctx))
    check("/spent без суммы: ничего не создано, «Не нашёл сумму»", len(txs()) == n and "Не нашёл сумму" in u.replies[-1].text)
    u = ic.tg_update(A["telegram_user_id"], text="/income получил зарплату 1800 евро")
    run(h.income_command(u, ctx))
    check("/income записывает доход", txs()[-1]["kind"] == "income" and "+1800.00 EUR" in u.replies[-1].text)

    u = ic.tg_update(A["telegram_user_id"], text="/currency KZT")
    run(h.currency_command(u, ctx))
    check("/currency KZT меняет валюту по умолчанию", __import__("money_service").default_currency(A["id"]) == "KZT")
    u = ic.tg_update(A["telegram_user_id"], text="/currency GBP")
    run(h.currency_command(u, ctx))
    check("/currency GBP отклоняется", "Такой валюты нет" in u.replies[-1].text)
    u = ic.tg_update(A["telegram_user_id"], text="/month")
    run(h.month_command(u, ctx))
    check("/month показывает итоги месяца по валютам", "EUR: расходы" in u.replies[-1].text, u.replies[-1].text[:80])
    u = ic.tg_update(A["telegram_user_id"], text="/last")
    run(h.last_command(u, ctx))
    check("/last показывает последние операции с кнопками «Удалить»",
          "Последние операции" in u.replies[-1].text and "Удалить 1" in str(u.replies[-1].markup))
    last_id = u.replies[-1].markup.inline_keyboard[0][0].callback_data
    p = press(B, last_id)
    check("«Удалить» из /last чужим пользователем ничего не удаляет", p.answers == ["Операция не найдена"]
          and any(t["id"] == last_id.split(":")[2] for t in db.tables["money_transactions"]))
    u = ic.tg_update(9_999_999, text="кофе 3 евро")
    n_all = len(db.tables["money_transactions"])
    run(h.handle_text(u, ctx))
    check("незнакомый чат ничего не создаёт", len(db.tables["money_transactions"]) == n_all and "Нет доступа" in u.replies[-1].text)

    def broken(*_a, **_k):
        raise RuntimeError("model_not_found")
    groq_client.parse_money_text = broken
    u = ic.tg_update(A["telegram_user_id"], text="кофе 3 евро")
    run(h.handle_text(u, ctx))
    check("ошибка модели: бот прямо говорит, что запись не создана", "запись не создана" in u.replies[-1].text)
    groq_client.parse_money_text = real_parse  # для --model нужен настоящий разбор


def privacy_and_log_checks():
    section("Приватность")
    text = open(os.path.join(ROOT, "web", "privacy.html"), encoding="utf-8").read()
    check("privacy.html упоминает учёт расходов и доходов", "expense" in text.lower() and "income" in text.lower())
    src = open(os.path.join(ROOT, "money_service.py"), encoding="utf-8").read()
    log_lines = [l for l in src.splitlines() if "logger." in l]
    check("money_service не пишет в лог суммы и заметки",
          not any(w in l for l in log_lines for w in ("amount", "note", "text)")), str(log_lines))


# ---------------------------------------------------------------- разбор настоящей моделью

def model_checks():
    import money_service

    section("Разбор фраз настоящей моделью (сегодня = 2026-10-07)")
    import users_service
    user = users_service.create_user("Модель", 7007)
    money_service.ensure_defaults(user["id"])
    money_service.today_for = lambda uid: date(2026, 10, 7)
    names = {c["name"] for c in money_service.list_categories(user["id"])}
    cases = [
        ("кофе 3,5 евро", [("expense", "3.50", "EUR", "2026-10-07")]),
        ("вчера такси 12 евро", [("expense", "12.00", "EUR", "2026-10-06")]),
        ("продукты 45 евро, проезд 1500 тенге", [("expense", "45.00", "EUR", None), ("expense", "1500.00", "KZT", None)]),
        ("получил зарплату 1800 евро", [("income", "1800.00", "EUR", None)]),
        ("аренда 650", [("expense", "650.00", "EUR", None)]),
        ("5к тенге такси", [("expense", "5000.00", "KZT", None)]),
    ]
    for phrase, expected in cases:
        try:
            got = money_service.drafts_from_text(user["id"], phrase)
        except Exception as exc:
            check(f"«{phrase}»", False, type(exc).__name__)
            continue
        strict = [(d["kind"], d["amount"], d["currency"], d["spent_on"] if e[3] else None) for d, e in zip(got, expected)]
        ok = len(got) == len(expected) and strict == [e for e in expected]
        check(f"«{phrase}» → {', '.join(f'{e[0]} {e[1]} {e[2]}' for e in expected)}", ok,
              str([(d["kind"], d["amount"], d["currency"], d["spent_on"]) for d in got]))
        print("      категории (мягкая проверка): " + ", ".join(d["category"] for d in got))
        check(f"«{phrase}»: категории существуют", all(d["category"] in names for d in got))
    check("«купить молоко» — не деньги", not money_service.looks_like_money("купить молоко"))
    check("«кофе» без суммы — ничего не создано", money_service.drafts_from_text(user["id"], "кофе") == [])
    from groq_client import classify_merchant
    expense_names = [c["name"] for c in money_service.list_categories(user["id"], "expense")]
    for merchant, expected in (("ESSELUNGA SPA", "Еда"), ("UBER *TRIP", "Транспорт"), ("Farmacia Centrale", "Здоровье"),
                               ("Spotify", "Подписки")):
        got = classify_merchant(merchant, expense_names)
        check(f"магазин «{merchant}» → {expected}", got == expected, str(got))


# ---------------------------------------------------------------- запуск

def run_local(with_model):
    real_groq = None
    if with_model:
        from dotenv import dotenv_values
        real_groq = os.environ.get("GROQ_API_KEY") or dotenv_values(os.path.join(ROOT, ".env")).get("GROQ_API_KEY")
        if not real_groq:
            sys.exit("Для --model нужен GROQ_API_KEY (значение не печатается)")
    db = ic.setup_local()
    if real_groq:
        os.environ["GROQ_API_KEY"] = real_groq
    from fastapi.testclient import TestClient

    import main
    import money_service
    import task_service
    import users_service

    task_service.parse_text_to_tasks = ic.fake_parse
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    A = users_service.create_user("Тест A", 2001)
    B = users_service.create_user("Тест B", 2002)
    for u in (A, B):
        ic.give_categories(u, "Work", "Personal")
        money_service.ensure_defaults(u["id"])
    client = TestClient(main.app, raise_server_exceptions=False)

    def db_insert(table, rows):
        db.table(table).insert(rows).execute()

    print("Локальная проверка денег (база в памяти, без сети)")
    http_checks(client, token, A, B, db_insert)
    wallet_checks(client, token, B)
    import groq_client
    import notifier
    sent = []
    notifier.notify_user = lambda user_id, text, with_app_button=False, buttons=None: sent.append((user_id, text, buttons)) or 1
    real_classify = groq_client.classify_merchant
    groq_client.classify_merchant = lambda merchant, cats: "Еда" if "esselunga" in merchant.lower() else None
    applepay_checks(client, token, A, B, True)
    groq_client.classify_merchant = real_classify  # для --model нужен настоящий подбор
    check("бот пишет о покупке с кнопками «Отменить» и «Категория»",
          sent and sent[0][1].startswith("💳 Apple Pay: ESSELUNGA SpA 123 · 23.40 EUR")
          and [b["text"] for b in sent[0][2][0]] == ["Отменить", "Категория"], str(sent[:1]))
    check("о повторе покупки бот не пишет второй раз", len([x for x in sent if "ESSELUNGA SpA 123" in x[1]]) == 1)
    limit_alerts = []
    notifier.notify_user = lambda user_id, text, with_app_button=False, buttons=None: limit_alerts.append(text) or 1
    limits_checks(client, token, B, limit_alerts)
    import money_service as ms
    from datetime import date as _date
    report = ms.monthly_report_text(B["id"], _date(2026, 6, 1))
    check("месячный отчёт: итоги июня", report and report.startswith("📊 Итоги: июнь 2026") and "Потрачено 0.30 EUR" in report, str(report)[:120])
    check("за месяц без операций отчёта нет", ms.monthly_report_text(B["id"], _date(2025, 1, 1)) is None)
    check("отчёт за месяц — один раз", ms.claim_monthly_report(B["id"], "2026-06") and not ms.claim_monthly_report(B["id"], "2026-06"))
    recurring_checks(db, client, A, B)
    release_checks(db)
    detection_checks()
    bot_checks(db, B, A)  # у B после HTTP-проверок нетронутые стартовые категории и валюта EUR
    privacy_and_log_checks()
    if with_model:
        model_checks()


def run_remote(base_url):
    import httpx
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
    for name in ("SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_BOT_TOKEN"):
        if not os.environ.get(name):
            sys.exit(f"Нужна переменная {name} (значение не печатается)")
    from database import supabase
    import money_service
    import users_service

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    base_tg = 9_100_000_000 + secrets.randbelow(10**6) * 10
    created = []
    print(f"Проверка денег против {base_url} (временные пользователи будут удалены)")
    try:
        for i, label in enumerate(("A", "B"), 1):
            u = users_service.create_user(f"money-check {label}", base_tg + i)
            created.append(u)
            money_service.ensure_defaults(u["id"])
        A, B = created

        def db_insert(table, rows):
            for start in range(0, len(rows), 500):
                supabase.table(table).insert(rows[start:start + 500]).execute()

        with httpx.Client(base_url=base_url, timeout=90) as client:
            client.get("/health")
            http_checks(client, token, A, B, db_insert)
            wallet_checks(client, token, B)
            applepay_checks(client, token, A, B, False)
            limits_checks(client, token, B, None)
    finally:
        for user in created:
            for table in ("money_transactions", "money_recurring", "money_merchant_rules", "money_limit_alerts",
                          "money_report_log", "money_categories", "categories", "telegram_chats"):
                supabase.table(table).delete().eq("user_id", user["id"]).execute()
            supabase.table("users").delete().eq("id", user["id"]).execute()
        print(f"\nВременные пользователи удалены: {len(created)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="адрес работающего сервиса; без него — локальная проверка")
    parser.add_argument("--model", action="store_true", help="локально: разбор фраз настоящей моделью Groq")
    args = parser.parse_args()
    if args.base_url:
        run_remote(args.base_url.rstrip("/"))
    else:
        run_local(args.model)
    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
