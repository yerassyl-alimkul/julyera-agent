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
                      "access_key": secrets.token_hex(32), "timezone": "Europe/Rome", "default_currency": "EUR", "digest_enabled": True, "remind_before": 30,
                      "created_at": _now()},
    "money_categories": lambda: {"id": str(uuid.uuid4()), "sort_order": 0, "monthly_limit": None, "limit_currency": None,
                                 "created_at": _now()},
    "money_limit_alerts": lambda: {"sent_at": _now()},
    "task_comments": lambda: {"id": str(uuid.uuid4()), "text": None, "file_path": None, "file_name": None,
                              "file_type": None, "file_size": None, "created_at": _now()},
    "money_report_log": lambda: {"sent_at": _now()},
    "money_transactions": lambda: {"id": str(uuid.uuid4()), "note": None, "source": "app", "recurring_id": None,
                                   "created_at": _now()},
    "money_recurring": lambda: {"id": str(uuid.uuid4()), "note": None, "active": True, "frequency": "monthly",
                                "anchor_date": None, "created_at": _now()},
    "money_merchant_rules": lambda: {"updated_at": _now()},
    "money_goals": lambda: {"id": str(uuid.uuid4()), "due_date": None, "achieved_at": None, "created_at": _now()},
    "money_goal_moves": lambda: {"id": str(uuid.uuid4()), "created_at": _now()},
    "release_log": lambda: {"sent_at": _now()},
    "tasks": lambda: {"id": str(uuid.uuid4()), "source": None, "deadline": None, "priority": "Mid",
                      "status": "To Do", "note": None, "is_recurring": False, "recurrence_pattern": None,
                      "tags": [], "google_event_id": None, "snoozed_until": None, "created_at": _now(), "updated_at": _now()},
    "digest_log": lambda: {"sent_at": _now()},
    "pre_reminder_log": lambda: {"sent_at": _now()},
    "notes": lambda: {"id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now()},
    "note_files": lambda: {"id": str(uuid.uuid4()), "created_at": _now()},
    "task_invites": lambda: {"id": str(uuid.uuid4()), "status": "pending", "answered_at": None, "created_at": _now()},
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
NUMERIC = {"money_transactions": ["amount"], "money_recurring": ["amount"], "money_categories": ["monthly_limit"],
           "money_goals": ["target"], "money_goal_moves": ["amount"]}  # PostgREST отдаёт numeric числом


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

    def neq(self, column, value):
        self.filters.append(lambda row: row.get(column) != value)
        return self

    def is_(self, column, value):  # только is null, как в коде
        assert value in ("null", None)
        self.filters.append(lambda row: row.get(column) is None)
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
        composite = {"money_limit_alerts": ("user_id", "category_id", "month", "threshold"),
                     "money_report_log": ("user_id", "month"), "pre_reminder_log": ("task_id", "deadline"),
                     "task_invites": ("task_id", "to_user")}
        if self.table in composite:  # составной первичный ключ
            cols = composite[self.table]
            for other in self._rows():
                if other is not row and all(other.get(c) == row.get(c) for c in cols):
                    raise FakeError(f'duplicate key value violates unique constraint "{self.table}_pkey"', "23505")
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
        if self.table == "tasks" and row.get("money_recurring_id") and row.get("status") != "Done":
            for other in self._rows():  # tasks_money_recurring_open_idx: одна открытая задача у платежа
                if other is not row and other.get("money_recurring_id") == row["money_recurring_id"] \
                        and other.get("status") != "Done":
                    raise FakeError('duplicate key value violates unique constraint "tasks_money_recurring_open_idx"', "23505")
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
            if self.table == "money_categories":  # правила магазинов и предупреждения о лимите — on delete cascade
                for ref in ("money_merchant_rules", "money_limit_alerts"):
                    self.db.tables[ref] = [x for x in self.db.tables.get(ref, []) if x["category_id"] not in ids]
            if self.table == "money_recurring":  # recurring_id ... on delete set null
                for x in self.db.tables.get("money_transactions", []):
                    if x.get("recurring_id") in ids:
                        x["recurring_id"] = None
                for x in self.db.tables.get("tasks", []):  # tasks.money_recurring_id ... on delete set null
                    if x.get("money_recurring_id") in ids:
                        x["money_recurring_id"] = None
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            if self.table == "tasks":  # on delete cascade для подзадач и комментариев
                ids = {r["id"] for r in gone}
                self.db.tables["subtasks"] = [s for s in self.db.tables.get("subtasks", []) if s["task_id"] not in ids]
                self.db.tables["task_comments"] = [c for c in self.db.tables.get("task_comments", []) if c["task_id"] not in ids]
                self.db.tables["task_invites"] = [c for c in self.db.tables.get("task_invites", []) if c["task_id"] not in ids]
            if self.table == "notes":  # note_files.note_id ... on delete cascade
                ids = {r["id"] for r in gone}
                self.db.tables["note_files"] = [f for f in self.db.tables.get("note_files", []) if f["note_id"] not in ids]
            if self.table == "money_goals":  # money_goal_moves.goal_id ... on delete cascade
                ids = {r["id"] for r in gone}
                self.db.tables["money_goal_moves"] = [m for m in self.db.tables.get("money_goal_moves", [])
                                                      if m["goal_id"] not in ids]
            if self.table == "users":  # categories.user_id и money_*.user_id ... on delete cascade
                ids = {r["id"] for r in gone}
                for t in ("categories", "money_transactions", "money_recurring", "money_merchant_rules", "money_goals",
                          "money_goal_moves",
                          "money_limit_alerts", "money_report_log", "money_categories", "digest_log", "task_comments"):
                    self.db.tables[t] = [c for c in self.db.tables.get(t, []) if c["user_id"] not in ids]
            return SimpleNamespace(data=[copy.deepcopy(r) for r in gone])

        raise FakeError(f"неизвестная операция {self.op}")


class FakeBucket:
    """Supabase Storage в памяти: upload / remove / create_signed_urls."""

    def __init__(self, files):
        self.files = files

    def upload(self, path, data, file_options=None):
        if path in self.files:
            raise FakeError("The resource already exists")
        self.files[path] = (bytes(data), (file_options or {}).get("content-type"))
        return SimpleNamespace(path=path)

    def remove(self, paths):
        for path in paths:
            self.files.pop(path, None)
        return [{"name": p} for p in paths]

    def create_signed_urls(self, paths, expires_in):
        # вместо ссылки на облако — сам файл в data:-ссылке, чтобы тестовый сервер показывал фото
        import base64
        return [{"path": p, "signedURL": f"data:{self.files[p][1]};base64,{base64.b64encode(self.files[p][0]).decode()}"}
                for p in paths if p in self.files]


class FakeStorage:
    def __init__(self):
        self.buckets: dict[str, dict] = {}

    def from_(self, bucket):
        return FakeBucket(self.buckets.setdefault(bucket, {}))


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.storage = FakeStorage()

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
