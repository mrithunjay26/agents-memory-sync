import os
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from store import db_path

SESSION_COOKIE = "amsession"
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def _secret_key_path() -> str:
    return os.path.join(os.path.dirname(db_path()), "secret_key")


def _get_or_create_secret_key() -> str:
    path = _secret_key_path()
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_get_or_create_secret_key(), salt="agent-memory-sync-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_session_cookie(username: str) -> str:
    return _serializer().dumps({"username": username})


def verify_session_cookie(cookie_value: str) -> str | None:
    try:
        data = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("username")
