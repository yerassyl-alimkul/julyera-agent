"""
Проверки миграции 015 и шагов: папки (личные и группы), задачи в папках, перенос между папками, права,
удаление папки; задача сама закрывается, когда отмечен последний шаг (и у общей задачи группы).

    python scripts/folders_check.py

Только локально: база в памяти, Telegram и Google подменены.
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

    import groups_service
    import main as app_main
    import notifier
    import telegram_handlers as h
    import users_service

    client = TestClient(app_main.app, raise_server_exceptions=False)
    groups_service.bot_username = lambda: "JulYeraTestBot"
    notifier.send_telegram = lambda chat_id, text, with_app_button=False, buttons=None: True
    Y = users_service.create_user("Yera", 9001)
    J = users_service.create_user("Julie", 9002)
    X = users_service.create_user("Чужой", 9003)
    for u in (Y, J, X):
        ic.give_categories(u, "Work")
        users_service.link_chat(u["telegram_user_id"], u["id"])
    hY, hJ, hX = ic.key_headers(Y), ic.key_headers(J), ic.key_headers(X)
    task = lambda tid: next((t for t in db.tables["tasks"] if t["id"] == tid), None)

    section("Личные папки")
    check("папок пока нет", client.get("/folders", headers=hY).json() == [])
    uni = client.post("/folders", headers=hY, json={"name": "Университет"}).json()
    check("папка создана", uni["name"] == "Университет" and uni["total"] == 0 and uni["group_id"] is None)
    check("та же папка второй раз → 409", client.post("/folders", headers=hY, json={"name": "университет"}).status_code == 409)
    check("пустое название → 422", client.post("/folders", headers=hY, json={"name": " "}).status_code == 422)
    check("чужие личные папки не видны", client.get("/folders", headers=hX).json() == [])
    t = client.post("/tasks", headers=hY, json={"task": "Курсовая", "category": "Work", "folder_id": uni["id"]}).json()
    check("задача в папке", t["folder_id"] == uni["id"])
    check("в чужую папку задачу не положить",
          client.post("/tasks", headers=hX, json={"task": "x", "category": "Work", "folder_id": uni["id"]}).status_code == 404)
    t2 = client.post("/tasks", headers=hY, json={"task": "Эссе", "category": "Work"}).json()
    r = client.patch(f"/tasks/{t2['id']}", headers=hY, json={"folder_id": uni["id"]})
    check("переложить задачу в папку", r.status_code == 200 and r.json()["folder_id"] == uni["id"])
    r = client.patch(f"/tasks/{t2['id']}", headers=hY, json={"folder_id": None})
    check("убрать из папки", r.json()["folder_id"] is None)
    check("счётчик задач папки", client.get("/folders", headers=hY).json()[0]["total"] == 1)
    check("переименовать", client.patch(f"/folders/{uni['id']}", headers=hY, json={"name": "Учёба"}).json()["name"] == "Учёба")
    check("чужой не переименует", client.patch(f"/folders/{uni['id']}", headers=hX, json={"name": "x"}).status_code == 404)

    section("Папки группы")
    g = client.post("/groups", headers=hY, json={"name": "JulYera", "tick_all": True}).json()
    token = g["invite"]["start"].split("g_")[1]
    cu = ic.tg_update(J["telegram_user_id"], callback_data=f"gj:y:{token}")
    asyncio.run(h.group_join_callback(cu, SimpleNamespace(bot=ic.FakeBot())))
    vienna = client.post("/folders", headers=hJ, json={"name": "Поездка в Вену", "group_id": g["id"]}).json()
    check("папку группы создаёт любой участник", vienna["group_id"] == g["id"])
    check("папки группы видят все участники", [f["name"] for f in client.get("/folders", headers=hY, params={"group_id": g["id"]}).json()]
          == ["Поездка в Вену"])
    check("чужой папки группы не видит", client.get("/folders", headers=hX, params={"group_id": g["id"]}).status_code == 404)
    check("папка группы не попадает в личные", client.get("/folders", headers=hY).json()[0]["name"] == "Учёба"
          and len(client.get("/folders", headers=hY).json()) == 1)
    vt = client.post("/tasks", headers=hY, json={"task": "Забронировать жильё", "category": "Work", "folder_id": vienna["id"]}).json()
    jcopy = next(t for t in db.tables["tasks"] if t["user_id"] == J["id"] and t["task"] == "Забронировать жильё")
    check("задача в папке группы — сама становится задачей группы, у Julie тоже в папке",
          vt["group_id"] == g["id"] and vt["category"] == "JulYera" and jcopy["folder_id"] == vienna["id"])
    check("личную задачу не положить в папку группы",
          client.patch(f"/tasks/{t['id']}", headers=hY, json={"folder_id": vienna["id"]}).status_code == 422)
    check("задачу группы не положить в личную папку",
          client.patch(f"/tasks/{vt['id']}", headers=hY, json={"folder_id": uni["id"]}).status_code == 422)
    other = client.post("/tasks", headers=hY, json={"task": "Ужин", "category": "Work", "group_id": g["id"]}).json()
    client.patch(f"/tasks/{other['id']}", headers=hY, json={"folder_id": vienna["id"]})
    check("переложили задачу группы в папку — у Julie тоже",
          next(t for t in db.tables["tasks"] if t["user_id"] == J["id"] and t["task"] == "Ужин")["folder_id"] == vienna["id"])
    check("удалить папку группы может её создатель или создатель группы, чужой — нет",
          client.delete(f"/folders/{vienna['id']}", headers=hX).status_code == 404)

    section("Шаги: последняя галочка закрывает задачу")
    s1 = client.post(f"/tasks/{vt['id']}/subtasks", headers=hY, json={"content": "Сравнить варианты"}).json()
    s2 = client.post(f"/tasks/{vt['id']}/subtasks", headers=hY, json={"content": "Написать хозяину"}).json()
    client.patch(f"/subtasks/{s1['id']}", headers=hY, json={"is_done": True})
    check("не все шаги — задача открыта", task(vt["id"])["status"] != "Done")
    client.patch(f"/subtasks/{s2['id']}", headers=hJ, json={"is_done": True})
    check("Julie отметила последний шаг — задача закрыта у обоих (галочка у всех)",
          task(vt["id"])["status"] == "Done" and task(jcopy["id"])["status"] == "Done")
    t3 = client.post("/tasks", headers=hY, json={"task": "Сдать отчёт", "category": "Work"}).json()
    client.post(f"/tasks/{t3['id']}/subtasks", headers=hY, json={"content": "Черновик"})
    r = client.patch(f"/tasks/{t3['id']}", headers=hY, json={"status": "Done"})
    check("закрыть задачу с незакрытыми шагами вручную можно", r.status_code == 200 and r.json()["status"] == "Done")

    section("Удаление папки")
    check("удаление личной папки", client.delete(f"/folders/{uni['id']}", headers=hY).status_code == 204)
    check("задачи остались без папки", task(t["id"])["folder_id"] is None)
    check("без входа → 401", client.get("/folders").status_code == 401)

    failed = [name for ok, name in ic.results if not ok]
    print(f"\nИтого: {len(ic.results) - len(failed)} из {len(ic.results)} проверок пройдено.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
