"""
База в памяти с тем же интерфейсом запросов, что у supabase-py (только то, чем пользуется приложение):
table().select().eq().in_().lte().order().insert().update().delete().upsert().execute().

Нужна для scripts/isolation_check.py в локальном режиме: проверки изоляции идут через настоящий
main.py и сервисы, но без сети и без настоящей базы. Повторяет важное поведение Postgres:
значения по умолчанию, уникальный telegram_user_id, первичные ключи для upsert,
каскадное удаление подзадач вместе с задачей.
"""
import copy
import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Значения по умолчанию и первичные ключи — как в schema.sql (после миграции multi-user).
DEFAULTS = {
    "users": lambda: {"id": str(uuid.uuid4()), "is_admin": False, "telegram_user_id": None,
                      "access_key": secrets.token_hex(32), "timezone": "Europe/Rome", "created_at": _now()},
    "tasks": lambda: {"id": str(uuid.uuid4()), "source": None, "deadline": None, "priority": "Mid",
                      "status": "To Do", "note": None, "is_recurring": False, "recurrence_pattern": None,
                      "tags": [], "google_event_id": None, "created_at": _now(), "updated_at": _now()},
    "notes": lambda: {"id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now()},
    "subtasks": lambda: {"id": str(uuid.uuid4()), "is_done": False, "created_at": _now()},
    "telegram_chats": lambda: {"created_at": _now()},
    "categories": lambda: {"id": str(uuid.uuid4()), "sort_order": 0, "created_at": _now()},
    "google_credentials": lambda: {"access_token": None, "token_expiry": None, "needs_reconnect": False,
                                   "created_at": _now(), "updated_at": _now()},
}
PRIMARY_KEYS = {"telegram_chats": "chat_id", "google_credentials": "user_id"}
UNIQUE = {"users": ["telegram_user_id", "access_key"]}
NOT_NULL = {"tasks": ["user_id", "task", "category"], "notes": ["user_id", "content"],
            "telegram_chats": ["user_id"], "google_credentials": ["user_id", "refresh_token"],
            "subtasks": ["task_id", "content"], "users": ["name"],
            "categories": ["user_id", "name", "color"]}


class FakeError(Exception):
    pass


class Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.filters, self.order_by, self.columns = "select", None, [], None, "*"

    # --- построение запроса ---
    def select(self, columns="*"):
        self.op, self.columns = "select", columns
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload = "upsert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, column, value):
        self.filters.append(lambda row: row.get(column) == value)
        return self

    def in_(self, column, values):
        values = list(values)
        self.filters.append(lambda row: row.get(column) in values)
        return self

    def lte(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and str(row.get(column)) <= str(value))
        return self

    def order(self, column, desc=False, nullsfirst=False, **_):
        self.order_by = (column, desc, nullsfirst)
        return self

    # --- выполнение ---
    def _rows(self):
        return self.db.tables.setdefault(self.table, [])

    def _match(self, row):
        return all(f(row) for f in self.filters)

    def _check(self, row):
        for col in NOT_NULL.get(self.table, []):
            if row.get(col) is None:
                raise FakeError(f'null value in column "{col}" of relation "{self.table}"')
        if self.table == "categories":  # unique index categories (user_id, lower(name))
            for other in self._rows():
                if other is not row and other["user_id"] == row["user_id"] and other["name"].lower() == row["name"].lower():
                    raise FakeError('duplicate key value violates unique constraint "categories_user_name_idx"')
        for col in UNIQUE.get(self.table, []):
            if row.get(col) is None:
                continue
            for other in self._rows():
                if other is not row and other.get(col) == row.get(col):
                    raise FakeError(f'duplicate key value violates unique constraint "{self.table}_{col}_key"')

    def _project(self, row):
        if self.columns == "*":
            return copy.deepcopy(row)
        return {c.strip(): copy.deepcopy(row.get(c.strip())) for c in self.columns.split(",")}

    def execute(self):
        rows = self._rows()
        if self.op == "select":
            found = [r for r in rows if self._match(r)]
            if self.order_by:
                col, desc, nullsfirst = self.order_by
                present = sorted([r for r in found if r.get(col) is not None], key=lambda r: str(r[col]), reverse=desc)
                nulls = [r for r in found if r.get(col) is None]
                found = nulls + present if nullsfirst else present + nulls
            return SimpleNamespace(data=[self._project(r) for r in found])

        if self.op in ("insert", "upsert"):
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for item in items:
                pk = PRIMARY_KEYS.get(self.table, "id")
                existing = next((r for r in rows if pk in item and r.get(pk) == item[pk]), None)
                if existing and self.op == "upsert":
                    existing.update(copy.deepcopy(item))
                    self._check(existing)
                    out.append(copy.deepcopy(existing))
                    continue
                if existing:
                    raise FakeError(f'duplicate key value violates unique constraint "{self.table}_pkey"')
                row = DEFAULTS.get(self.table, dict)()
                row.update(copy.deepcopy(item))
                self._check(row)
                rows.append(row)
                out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)

        if self.op == "update":
            out = []
            for row in rows:
                if self._match(row):
                    row.update(copy.deepcopy(self.payload))
                    self._check(row)
                    out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)

        if self.op == "delete":
            gone = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            if self.table == "tasks":  # on delete cascade для подзадач
                ids = {r["id"] for r in gone}
                self.db.tables["subtasks"] = [s for s in self.db.tables.get("subtasks", []) if s["task_id"] not in ids]
            if self.table == "users":  # categories.user_id ... on delete cascade
                ids = {r["id"] for r in gone}
                self.db.tables["categories"] = [c for c in self.db.tables.get("categories", []) if c["user_id"] not in ids]
            return SimpleNamespace(data=[copy.deepcopy(r) for r in gone])

        raise FakeError(f"неизвестная операция {self.op}")


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, params):
        """SQL-функции из schema.sql. rename_category — как в миграции: задачи и категория разом."""
        db = self

        class Call:
            def execute(self):
                if name != "rename_category":
                    raise FakeError(f"нет функции {name}")
                cats = db.tables.get("categories", [])
                cat = next((c for c in cats if c["id"] == params["p_id"] and c["user_id"] == params["p_user"]), None)
                if cat is None:
                    raise FakeError("not_found")
                old_name = cat["name"]
                for t in db.tables.get("tasks", []):
                    if t["user_id"] == params["p_user"] and t["category"] == old_name:
                        t["category"] = params["p_name"]
                cat["name"] = params["p_name"]
                return SimpleNamespace(data=None)

        return Call()
