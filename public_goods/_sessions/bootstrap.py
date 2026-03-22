from __future__ import annotations

import sqlite3
from typing import Optional

from ..config import ADMIN_PASSWORD, PASSWORD_SCHEME_LEGACY_ADMIN, USER_ROLE_ADMIN
from ..db import _get_setting_conn
from .users import _create_user_conn
from .._auth.usernames import _normalize_username


def _ensure_bootstrap_admin(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    admin = conn.execute(
        "SELECT * FROM users WHERE username_norm=?",
        (_normalize_username("admin"),),
    ).fetchone()
    if admin:
        return admin

    legacy_hash = (_get_setting_conn(conn, "admin_password_hash") or "").strip()
    if legacy_hash:
        return _create_user_conn(
            conn,
            "admin",
            USER_ROLE_ADMIN,
            password_scheme=PASSWORD_SCHEME_LEGACY_ADMIN,
            password_hash=legacy_hash,
            password_salt="",
        )

    if ADMIN_PASSWORD:
        return _create_user_conn(conn, "admin", USER_ROLE_ADMIN, password=ADMIN_PASSWORD)

    return None


def _backfill_session_owners(conn: sqlite3.Connection, admin_user_id: str) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET owner_user_id=?
        WHERE owner_user_id IS NULL OR TRIM(owner_user_id)=''
    """,
        (admin_user_id,),
    )


def _backfill_session_join_tokens(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET join_token=id
        WHERE join_token IS NULL OR TRIM(join_token)=''
    """
    )


def _get_admin_user_conn(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE username_norm=?",
        (_normalize_username("admin"),),
    ).fetchone()

