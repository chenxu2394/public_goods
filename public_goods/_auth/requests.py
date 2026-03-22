from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from ..config import AUTH_COOKIE_NAME, USER_ROLE_ADMIN
from .tokens import verify_auth_token


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    from .._sessions.users import get_user_by_id

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

