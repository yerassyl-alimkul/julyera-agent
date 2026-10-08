"""
База в памяти с тем же интерфейсом запросов, что у supabase-py (только то, чем пользуется приложение):
table().select().eq().in_().lte().order().insert().update().delete().upsert().execute().

Нужна для scripts/isolation_check.py в локальном режиме: проверки изоляции идут через настоящий
main.py и сервисы, но без сети и без настоящей базы. Повторяет важное поведение Postgres:
значения по умолчанию, уникальный telegram_user_id, первичные ключи для upsert,
каскадное удаление подзадач вместе с задачей, таблицы money_* (уникальные индексы, numeric как число,
запрет удалить категорию, на которую ссылаются операции).
"""
import copy
import random
import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Значения по умолчанию и первичные ключи — как в schema.sql (после миграции multi-user).
DEFAULTS = {
    "users": lambda: {"id": str(uuid.uuid4()), "is_admin": False, "telegram_user_id": None,
                      "access_key": secrets.token_hex(32), "timezone": "Europe/Rome", "default_currency": "EUR", "digest_enabled": True,
                      "created_at": _now()},
    "money_categories": lambda: {"id": str(uuid.uuid4()), "sort_order": 0, "created_at": _now()},
    "money_transactions": lambda: {"id": str(uuid.uuid4()), "note": None, "source": "app", "recurring_id": None,
                                   "created_at": _now()},
    "money_recurring": lambda: {"id": str(uuid.uuid4()), "note": None, "active": True, "frequency": "monthly",
                                "anchor_date": None, "created_at": _now()},
    "money_merchant_rules": lambda: {"updated_at": _now()},
    "release_log": lambda: {"sent_at": _now()},
    "tasks": lambda: {"id": str(uuid.uuid4()), "source": None, "deadline": None, "priority": "Mid",
                      "status": "To Do", "note": None, "is_recurring": False, "recurrence_pattern": None,
                      "tags": [], "google_event_id": None, "snoozed_until": None, "created_at": _now(), "updated_at": _now()},
    "digest_log": lambda: {"sent_at": _now()},
    "notes": lambda: {"id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now()},
    "subtasks": lambda: {"id": str(uuid.uuid4()), "is_done": False, "created_at": _now()},
    "telegram_chats": lambda: {"created_at": _now()},
    "categories": lambda: {"id": str(uuid.uuid4()), "sort_order": 0, "created_at": _now()},
    "google_credentials": lambda: {"access_token": None, "token_expiry": None, "needs_reconnect": False,
                                   "created_at": _now(), "updated_at": _now()},
}
PRIMARY_KEYS = {"telegram_chats": "chat_id", "google_credentials": "user_id", "release_log": "id"}
UNIQUE = {"users": ["telegram_user_id", "access_key"]}
NOT_NULL = {"tasks": ["user_id", "task", "category"], "notes": ["user_id", "content"],
            "telegram_chats": ["user_id"], "google_credentials": ["user_id", "refresh_token"],
            "subtasks": ["task_id", "content"], "users": ["name"],
            "categories": ["user_id", "name", "color"],
            "money_categories": ["user_id", "kind", "name", "color"],
            "money_transactions": ["user_id", "kind", "amount", "currency", "category_id", "spent_on"],
            "money_recurring": ["user_id", "kind", "amount", "currency", "category_id", "day_of_month", "next_run"]}
NUMERIC = {"money_transactions": ["amount"], "money_recurring": ["amount"]}  # PostgREST отдаёт numeric числом


class FakeError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.filters, self.order_by, self.columns = "select", None, [], [], "*"
        self.window = None  # (начало, конец включительно) из range()/limit()

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

    def gte(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and str(row.get(column)) >= str(value))
        return self

    def lt(self, column, value):
        self.filters.append(lambda row: row.get(column) is not None and str(row.get(column)) < str(value))
        return self

    def order(self, column, desc=False, nullsfirst=False, **_):
        self.order_by.append((column, desc, nullsfirst))
        return self

    def range(self, start, end):
        self.window = (start, end)
        return self

    def limit(self, n):
        self.window = (0, n - 1)
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
        if self.table == "digest_log":  # primary key (user_id, kind, day)
            for other in self._rows():
                if other is not row and (other["user_id"], other["kind"], other["day"]) == (row["user_id"], row["kind"], row["day"]):
                    raise FakeError('duplicate key value violates unique constraint "digest_log_pkey"', "23505")
        if self.table == "money_categories":  # unique (user_id, kind, lower(name))
            for other in self._rows():
                if other is not row and other["user_id"] == row["user_id"] and other["kind"] == row["kind"] \
                        and other["name"].lower() == row["name"].lower():
                    raise FakeError('duplicate key value violates unique constraint "money_categories_user_kind_name_idx"', "23505")
        if self.table == "money_transactions" and row.get("recurring_id"):  # unique (recurring_id, spent_on)
            for other in self._rows():
                if other is not row and other.get("recurring_id") == row["recurring_id"] \
                        and other["spent_on"] == row["spent_on"]:
                    raise FakeError('duplicate key value violates unique constraint "money_transactions_recurring_once_idx"', "23505")
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

    def _numeric(self, row):
        for col in NUMERIC.get(self.table, []):
            if row.get(col) is not None:
                row[col] = float(row[col])

    def _project(self, row):
        if self.columns == "*":
            return copy.deepcopy(row)
        return {c.strip(): copy.deepcopy(row.get(c.strip())) for c in self.columns.split(",")}

    def execute(self):
        rows = self._rows()
        if self.op == "select":
            found = [r for r in rows if self._match(r)]
            if self.window and self.order_by:
                # Postgres не обещает порядок строк с равными ключами сортировки: перемешиваем,
                # чтобы постраничные выборки без однозначного порядка ловились и здесь
                found = random.sample(found, len(found))
            for col, desc, nullsfirst in reversed(self.order_by):  # устойчивая сортировка: первый ключ главный
                present = sorted([r for r in found if r.get(col) is not None], key=lambda r: str(r[col]), reverse=desc)
                nulls = [r for r in found if r.get(col) is None]
                found = nulls + present if nullsfirst else present + nulls
            if self.window:
                found = found[self.window[0]:self.window[1] + 1]
            return SimpleNamespace(data=[self._project(r) for r in found])

        if self.op in ("insert", "upsert"):
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            batch_time = _now()  # как now() в Postgres: у всех строк одного insert одно время создания
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
                if "created_at" in row:
                    row["created_at"] = batch_time
                row.update(copy.deepcopy(item))
                self._numeric(row)
                self._check(row)
                rows.append(row)
                out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)

        if self.op == "update":
            out = []
            for row in rows:
                if self._match(row):
                    before = copy.deepcopy(row)
                    row.update(copy.deepcopy(self.payload))
                    self._numeric(row)
                    try:
                        self._check(row)
                    except FakeError:
                        row.clear()
                        row.update(before)
                        raise
                    out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)

        if self.op == "delete":
            gone = [r for r in rows if self._match(r)]
            ids = {r.get("id") for r in gone}
            if self.table == "money_categories":  # ссылки без каскада: Postgres не даст удалить используемую
                for ref in ("money_transactions", "money_recurring"):
                    if any(x["category_id"] in ids for x in self.db.tables.get(ref, [])):
                        raise FakeError(f'violates foreign key constraint on table "{ref}"', "23503")
            if self.table == "money_recurring":  # recurring_id ... on delete set null
                for x in self.db.tables.get("money_transactions", []):
                    if x.get("recurring_id") in ids:
                        x["recurring_id"] = None
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            if self.table == "tasks":  # on delete cascade для подзадач
                ids = {r["id"] for r in gone}
                self.db.tables["subtasks"] = [s for s in self.db.tables.get("subtasks", []) if s["task_id"] not in ids]
            if self.table == "users":  # categories.user_id и money_*.user_id ... on delete cascade
                ids = {r["id"] for r in gone}
                for t in ("categories", "money_transactions", "money_recurring", "money_categories", "digest_log"):
                    self.db.tables[t] = [c for c in self.db.tables.get(t, []) if c["user_id"] not in ids]
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
