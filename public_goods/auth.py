from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import (
    AUTH_COOKIE_NAME,
    AUTH_TOKEN_TTL_SECONDS,
    PASSWORD_HASH_ITERATIONS,
    PASSWORD_SCHEME_LEGACY_ADMIN,
    PASSWORD_SCHEME_PBKDF2,
    SECRET_KEY,
    USER_ROLE_ADMIN,
)


def _normalize_username(username: str) -> str:
    return username.strip().casefold()


def _validate_username(username: str) -> str:
    value = username.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if len(value) < 3 or len(value) > 32:
        raise HTTPException(400, "Username must be 3-32 characters.")
    if any(ch not in allowed for ch in value):
        raise HTTPException(400, "Username may contain only letters, numbers, '.', '_' or '-'.")
    return value


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(message: bytes) -> str:
    mac = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    return _b64url(mac)


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


def _is_auth_configured() -> bool:
    from .sessions import get_user_by_username

    return bool(SECRET_KEY) and get_user_by_username("admin") is not None


def _auth_configuration_error() -> str:
    if not SECRET_KEY:
        return "Set SECRET_KEY to sign login cookies."
    return "No admin account is available. On first boot set ADMIN_PASSWORD so the app can create one."


def _must_configure_auth() -> None:
    if not _is_auth_configured():
        raise HTTPException(500, _auth_configuration_error())


def make_auth_token(user: sqlite3.Row) -> str:
    _must_configure_auth()
    payload = {
        "uid": str(user["id"]),
        "role": str(user["role"]),
        "pv": int(user["password_version"]),
        "ts": int(dt.datetime.now().timestamp() * 1000),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _sign(body.encode("utf-8"))
    return f"{body}.{sig}"


def verify_auth_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        _must_configure_auth()
        if not token or "." not in token:
            return None
        body, sig = token.split(".", 1)
        expected = _sign(body.encode("utf-8"))
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        ts = int(payload.get("ts", 0))
        now_ts = int(dt.datetime.now().timestamp() * 1000)
        if ts <= 0 or now_ts - ts > AUTH_TOKEN_TTL_SECONDS * 1000:
            return None
        if not payload.get("uid") or not payload.get("role"):
            return None
        payload["pv"] = int(payload.get("pv", 0))
        if payload["pv"] <= 0:
            return None
        return payload
    except Exception:
        return None


def generate_temp_password(length: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    from .sessions import get_user_by_id

    if getattr(request.state, "_auth_loaded", False):
        return getattr(request.state, "_auth_user", None)

    request.state._auth_loaded = True
    request.state._auth_user = None

    payload = verify_auth_token(request.cookies.get(AUTH_COOKIE_NAME, ""))
    if not payload:
        return None

    user = get_user_by_id(str(payload["uid"]))
    if not user:
        return None
    if user["disabled_at"] is not None:
        return None
    if str(user["role"]) != str(payload["role"]):
        return None
    if int(user["password_version"]) != int(payload["pv"]):
        return None

    request.state._auth_user = user
    return user


def _auth_gate(request: Request) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse(url="/admin/login", status_code=303)
    return user, None


def _management_gate(request: Request, *, admin_only: bool = False) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    user, gate = _auth_gate(request)
    if gate:
        return None, gate
    if admin_only and user["role"] != USER_ROLE_ADMIN:
        raise HTTPException(404, "Not found")
    if int(user["must_change_password"]) == 1:
        return user, RedirectResponse(url="/admin?pw_change_required=1", status_code=303)
    return user, None
