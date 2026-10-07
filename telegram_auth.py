"""
Проверка данных входа Telegram Mini App (initData).

Telegram передаёт в Mini App строку initData вида "query_id=...&user=%7B...%7D&auth_date=...&hash=...".
Подпись считается так (документация Telegram Mini Apps, «Validating data received via the Mini App»):
    secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash       = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
где data_check_string — все пары, кроме hash, отсортированные по ключу и соединённые через "\n".
"""
import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """initData не прошла проверку: нет подписи, подпись неверна, данные устарели или повреждены.
    reason — короткая причина для логов: «нет данных», «подпись неверна», «устарел»."""

    def __init__(self, message: str, reason: str = "подпись неверна"):
        super().__init__(message)
        self.reason = reason


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _data_check_string(fields: dict) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields))


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400, now: Optional[float] = None) -> dict:
    """
    Проверяет подпись и свежесть initData и возвращает данные пользователя Telegram
    (словарь с ключами id, first_name, username и т.д.). При любом отклонении бросает InitDataError.
    """
    if not init_data or not bot_token:
        raise InitDataError("нет данных для проверки", reason="нет данных")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("строка initData повреждена") from exc

    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataError("повторяющиеся поля")
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("нет подписи")

    expected_hash = hmac.new(
        _secret_key(bot_token), _data_check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataError("подпись не совпадает")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("нет auth_date") from exc
    current = time.time() if now is None else now
    if current - auth_date > max_age:
        raise InitDataError("данные устарели", reason="устарел")
    if auth_date - current > 300:  # дата из будущего: часы не могут расходиться так сильно
        raise InitDataError("auth_date в будущем", reason="устарел")

    try:
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("нет данных пользователя") from exc
    user["id"] = user_id
    return user


def sign_init_data(fields: dict, bot_token: str) -> str:
    """
    Подписывает поля так же, как Telegram, и возвращает готовую строку initData.
    Нужна только для проверок (scripts/isolation_check.py); в работе приложения не используется.
    """
    from urllib.parse import urlencode

    data = {key: str(value) for key, value in fields.items()}
    data["hash"] = hmac.new(
        _secret_key(bot_token), _data_check_string(data).encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(data)
