from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import RedirectResponse

from ..auth import _auth_gate, _management_gate
from .._sessions import get_session_for_user


def require_authenticated_user(request: Request) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    return _auth_gate(request)


def require_management_user(request: Request) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    return _management_gate(request)


def require_admin_user(request: Request) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    return _management_gate(request, admin_only=True)


def load_session_for_user(session_id: str, user: sqlite3.Row) -> sqlite3.Row:
    return get_session_for_user(session_id, user)


def require_management_session(
    request: Request,
    session_id: str,
    *,
    admin_only: bool = False,
) -> Tuple[Optional[sqlite3.Row], Optional[sqlite3.Row], Optional[RedirectResponse]]:
    user, gate = require_admin_user(request) if admin_only else require_management_user(request)
    if gate or user is None:
        return None, None, gate
    return user, load_session_for_user(session_id, user), None
