from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from typing import Tuple

from ..config import (
    PASSWORD_HASH_ITERATIONS,
    PASSWORD_SCHEME_LEGACY_ADMIN,
    PASSWORD_SCHEME_PBKDF2,
    SECRET_KEY,
    USER_ROLE_ADMIN,
)
from .tokens import _b64url, _b64url_decode


def _generate_password_salt() -> str:
    return _b64url(secrets.token_bytes(16))


def _hash_password_with_salt(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _b64url_decode(salt),
        PASSWORD_HASH_ITERATIONS,
    )
    return _b64url(dk)


def _hash_password_record(password: str) -> Tuple[str, str]:
    salt = _generate_password_salt()
    return _hash_password_with_salt(password, salt), salt


def _legacy_hash_password(password: str) -> str:
    if not SECRET_KEY:
        return ""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        SECRET_KEY.encode("utf-8"),
        100_000,
    )
    return _b64url(dk)


def verify_user_password(user: sqlite3.Row, entered: str) -> Tuple[bool, bool]:
    scheme = str(user["password_scheme"] or "")
    if scheme == PASSWORD_SCHEME_PBKDF2:
        salt = str(user["password_salt"] or "")
        expected = _hash_password_with_salt(entered, salt)
        return hmac.compare_digest(str(user["password_hash"] or ""), expected), False
    if scheme == PASSWORD_SCHEME_LEGACY_ADMIN and user["role"] == USER_ROLE_ADMIN:
        expected = _legacy_hash_password(entered)
        return hmac.compare_digest(str(user["password_hash"] or ""), expected), True
    return False, False


def generate_temp_password(length: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))

