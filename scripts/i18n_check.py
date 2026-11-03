"""
Проверки языков сервера (миграция 017): бот, сводки, уведомления и ошибки API на русском, английском и казахском;
язык запоминается (PATCH /me, бот по языку Telegram), уведомление другому человеку — на его языке.

    python scripts/i18n_check.py

Только локально: база в памяти, Telegram подменён.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import isolation_check as ic  # noqa: E402

check, section = ic.check, ic.section


def main():
    db = ic.setup_local()
    from fastapi.testclient import TestClient

    import digest_service
    import i18n
    import i18n_server_keys
    import main as app_main
    import notifier
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    sent = []
    notifier.send_telegram = lambda chat_id, text, with_app_button=False, buttons=None: sent.append((chat_id, text, buttons)) or True

    section("Словарь")
    en, kk = i18n_server_keys.load()
    keys = i18n_server_keys.collect()
    check("у всех фраз есть английский и казахский", all(k in en and k in kk for k in keys))
    i18n.use("en")
    check("tr с подстановкой", i18n.tr("Категория: {0}", "Food") == "Category: Food")
    check("пробелы и переводы строк по краям сохраняются", i18n.tr("\nЗавтра:") == "\nTomorrow:")
    check("tn по-английски", i18n.tn(1, "задача|задачи|задач") in ("задача", "task"))
    i18n.use("ru")
    check("tn по-русски: 1 задача, 3 задачи, 5 задач",
          [i18n.tn(n, "задача|задачи|задач") for n in (1, 3, 5, 11, 21)] == ["задача", "задачи", "задач", "задач", "задача"])
    i18n.use("xx")
    check("неизвестный язык → русский", i18n.current() == "ru")

    O = users_service.create_user("Yera", 9201)
    E = users_service.create_user("Emma", 9202)
    K = users_service.create_user("Айгерім", 9203)
    for u in (O, E, K):
        ic.give_categories(u, "Work")
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hO, hE = ic.key_headers(O), ic.key_headers(E)

    section("API: язык ошибок по заголовку X-Lang и сохранение языка")
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.patch(f"/tasks/{missing}", headers={**hO, "X-Lang": "en"}, json={"task": "x"})
    check("X-Lang: en → «Task not found»", r.status_code == 404 and r.json()["detail"] == "Task not found")
    r = client.patch(f"/tasks/{missing}", headers={**hO, "X-Lang": "kk"}, json={"task": "x"})
    check("X-Lang: kk → «Тапсырма табылмады»", r.json()["detail"] == "Тапсырма табылмады")
    r = client.patch(f"/tasks/{missing}", headers=hO, json={"task": "x"})
    check("без заголовка — по-русски", r.json()["detail"] == "Задача не найдена")
    check("язык одного запроса не протекает в следующий",
          client.post("/categories", headers=hO, json={"name": "", "color": "#A9C89D"}).json()["detail"]["message"].startswith("Название"))
    r = client.patch("/me", headers=hE, json={"language": "en"})
    check("PATCH /me language → сохранён", r.status_code == 200 and r.json()["language"] == "en"
          and users_service.get_by_id(E["id"])["language"] == "en")
    check("неизвестный язык → 422", client.patch("/me", headers=hE, json={"language": "de"}).status_code == 422)
    client.patch("/me", headers=ic.key_headers(K), json={"language": "kk"})

    section("Бот")
    u = ic.tg_update(E["telegram_user_id"], text="/start")
    asyncio.run(h.start(u, SimpleNamespace(bot=ic.FakeBot(), args=[])))
    check("/start по-английски", u.replies[0].text.startswith("Hi, Emma!"))
    kb = u.replies[0].markup
    check("панель кнопок по-английски", kb.keyboard[0][0].text == "📋 Today")
    check("английская кнопка панели узнаётся", h.MENU_ACTIONS.get("📋 Today") == "today_command"
          and h.MENU_ACTIONS.get("📋 Бүгін") == "today_command" and h.MENU_ACTIONS.get("📋 Сегодня") == "today_command")
    u = ic.tg_update(K["telegram_user_id"], text="/list")
    asyncio.run(h.list_tasks_handler(u, SimpleNamespace(bot=ic.FakeBot(), args=[])))
    check("/list по-казахски", u.replies[0].text == "Белсенді тапсырма жоқ 🎉")
    u = ic.tg_update(O["telegram_user_id"], text="/list")
    asyncio.run(h.list_tasks_handler(u, SimpleNamespace(bot=ic.FakeBot(), args=[])))
    check("/list по-русски у того, кто язык не выбирал", u.replies[0].text == "Активных задач нет 🎉")
    N = users_service.create_user("New", 9204)
    u = ic.tg_update(N["telegram_user_id"], text="/list")
    u.effective_user.language_code = "kk"
    asyncio.run(h.list_tasks_handler(u, SimpleNamespace(bot=ic.FakeBot(), args=[])))
    check("язык Telegram без выбора в приложении → ответ на нём и запоминается",
          u.replies[0].text.startswith("Белсенді") and users_service.get_by_id(N["id"])["language"] == "kk")
    check("меню команд на трёх языках", h.bot_commands("en")[0].description == "All commands with buttons"
          and h.bot_commands("kk")[0].description == "Барлық команда түймелерімен")

    section("Уведомления и рассылки — на языке получателя")
    sent.clear()
    i18n.use("ru")
    notifier.notify_user(K["id"], i18n.L("👥 {0} теперь в группе «{1}».", "Yera", "Семья"), with_app_button=True)
    notifier.notify_user(E["id"], i18n.L("👥 {0} теперь в группе «{1}».", "Yera", "Семья"))
    notifier.notify_user(O["id"], i18n.L("👥 {0} теперь в группе «{1}».", "Yera", "Семья"))
    texts = [t for _, t, _ in sent]
    check("казахский, английский и русский получатели", texts == ["👥 Yera енді «Семья» тобында.",
                                                                 "👥 Yera is now in the group \"Семья\".",
                                                                 "👥 Yera теперь в группе «Семья»."])
    check("отложенный текст — обычная строка по-русски", str(i18n.L("Отменить")) == "Отменить")
    check("контекст после рассылки не изменился", i18n.current() == "ru")
    t = client.post("/tasks", headers=hE, json={"task": "Call Anna", "category": "Work",
                                                  "deadline": "2026-11-03T18:00:00"}).json()
    digest_service.utc_now = lambda: datetime(2026, 11, 3, 7, 5, tzinfo=timezone.utc)  # 08:05 в Риме
    user = users_service.get_by_id(E["id"])
    msgs = digest_service.due_messages(user)
    check("утренняя сводка по-английски, месяц тоже", msgs and msgs[0][0].startswith("☀️ Good morning! Plan for November 3"),
          msgs[0][0][:60] if msgs else msgs)
    check("после сводки язык вернулся", i18n.current() == "ru")
    check("задача из сводки на месте", t["task"] in msgs[0][0])

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
