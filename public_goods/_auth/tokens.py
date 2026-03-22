from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import sqlite3
from typing import Any, Dict, Optional

from fastapi import HTTPException

from ..config import AUTH_TOKEN_TTL_SECONDS, SECRET_KEY


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(message: bytes) -> str:
    mac = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    return _b64url(mac)


def _is_auth_configured() -> bool:
    from .._sessions.users import get_user_by_username

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

