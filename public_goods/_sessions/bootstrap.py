from __future__ import annotations

import sqlite3
from typing import Optional

from ..config import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    AUTH_MODE,
    AUTH_MODE_EASY_AUTH,
    PASSWORD_SCHEME_LEGACY_ADMIN,
    PASSWORD_SCHEME_MICROSOFT,
    USER_ROLE_ADMIN,
)
from ..db import _get_setting_conn, now_iso
from .users import _create_user_conn
from .._auth.usernames import _normalize_email, _normalize_username


def _ensure_bootstrap_admin(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    admin = conn.execute(
        "SELECT * FROM users WHERE username_norm=?",
        (_normalize_username("admin"),),
    ).fetchone()
    if admin:
        if (
            AUTH_MODE == AUTH_MODE_EASY_AUTH
            and ADMIN_EMAIL
            and admin["identity_subject"] is None
            and admin["email_norm"] != ADMIN_EMAIL
        ):
            conn.execute(
                """
                UPDATE users
                SET email=?, email_norm=?, updated_at=?
                WHERE id=?
                """,
                (ADMIN_EMAIL, _normalize_email(ADMIN_EMAIL), now_iso(), admin["id"]),
            )
            return conn.execute("SELECT * FROM users WHERE id=?", (admin["id"],)).fetchone()
        return admin

    if AUTH_MODE == AUTH_MODE_EASY_AUTH and ADMIN_EMAIL:
        return _create_user_conn(
            conn,
            "admin",
            USER_ROLE_ADMIN,
            password_scheme=PASSWORD_SCHEME_MICROSOFT,
            password_hash="",
            password_salt="",
            email=ADMIN_EMAIL,
        )

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
