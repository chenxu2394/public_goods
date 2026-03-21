from __future__ import annotations

import secrets
import sqlite3
from typing import List, Optional

from fastapi import HTTPException

from ..auth import _hash_password_record, _legacy_hash_password, _normalize_username, _validate_username
from ..config import (
    ADMIN_PASSWORD,
    PASSWORD_SCHEME_LEGACY_ADMIN,
    PASSWORD_SCHEME_PBKDF2,
    USER_ROLE_ADMIN,
    USER_ROLE_TEACHER,
)
from ..db import _get_setting_conn, db, now_iso


def _create_user_conn(
    conn: sqlite3.Connection,
    username: str,
    role: str,
    *,
    password: Optional[str] = None,
    password_scheme: Optional[str] = None,
    password_hash: Optional[str] = None,
    password_salt: Optional[str] = None,
    password_version: int = 1,
    must_change_password: bool = False,
) -> sqlite3.Row:
    if role not in {USER_ROLE_ADMIN, USER_ROLE_TEACHER}:
        raise ValueError("Invalid user role")
    username = _validate_username(username)
    username_norm = _normalize_username(username)
    if password is not None:
        password_hash, password_salt = _hash_password_record(password)
        password_scheme = PASSWORD_SCHEME_PBKDF2
    if not password_scheme or password_hash is None or password_salt is None:
        raise ValueError("Password material is required")

    now = now_iso()
    user_id = secrets.token_urlsafe(8)
    conn.execute(
        """
        INSERT INTO users(
            id, username, username_norm, role, password_scheme, password_hash, password_salt,
            password_version, must_change_password, disabled_at, created_at, updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            user_id,
            username,
            username_norm,
            role,
            password_scheme,
            password_hash,
            password_salt,
            int(password_version),
            1 if must_change_password else 0,
            None,
            now,
            now,
        ),
    )
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def _set_user_password_conn(
    conn: sqlite3.Connection,
    user_id: str,
    new_password: str,
    *,
    must_change_password: bool,
) -> None:
    password_hash, password_salt = _hash_password_record(new_password)
    conn.execute(
        """
        UPDATE users
        SET password_scheme=?, password_hash=?, password_salt=?,
            password_version=password_version + 1,
            must_change_password=?, updated_at=?
        WHERE id=?
    """,
        (
            PASSWORD_SCHEME_PBKDF2,
            password_hash,
            password_salt,
            1 if must_change_password else 0,
            now_iso(),
            user_id,
        ),
    )


def _rehash_legacy_user_password_conn(conn: sqlite3.Connection, user_id: str, entered_password: str) -> None:
    password_hash, password_salt = _hash_password_record(entered_password)
    conn.execute(
        """
        UPDATE users
        SET password_scheme=?, password_hash=?, password_salt=?, updated_at=?
        WHERE id=?
    """,
        (
            PASSWORD_SCHEME_PBKDF2,
            password_hash,
            password_salt,
            now_iso(),
            user_id,
        ),
    )
    conn.execute("DELETE FROM settings WHERE key IN ('admin_password_hash', 'password_changed_at')")


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


def get_user_by_id(user_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    username_norm = _normalize_username(username)
    conn = db()
    try:
        return conn.execute("SELECT * FROM users WHERE username_norm=?", (username_norm,)).fetchone()
    finally:
        conn.close()


def list_teachers(*, active_only: bool = False) -> List[sqlite3.Row]:
    conn = db()
    try:
        if active_only:
            return conn.execute(
                """
                SELECT *
                FROM users
                WHERE role=? AND disabled_at IS NULL
                ORDER BY username_norm ASC
            """,
                (USER_ROLE_TEACHER,),
            ).fetchall()
        return conn.execute(
            """
            SELECT *
            FROM users
            WHERE role=?
            ORDER BY username_norm ASC
        """,
            (USER_ROLE_TEACHER,),
        ).fetchall()
    finally:
        conn.close()


def create_user(username: str, role: str, password: str, *, must_change_password: bool) -> sqlite3.Row:
    conn = db()
    try:
        user = _create_user_conn(
            conn,
            username,
            role,
            password=password,
            must_change_password=must_change_password,
        )
        conn.commit()
        return user
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_password(user_id: str, new_password: str, *, must_change_password: bool) -> None:
    conn = db()
    try:
        _set_user_password_conn(
            conn,
            user_id,
            new_password,
            must_change_password=must_change_password,
        )
        conn.commit()
    finally:
        conn.close()


def set_user_disabled(user_id: str, disabled: bool) -> None:
    conn = db()
    try:
        conn.execute(
            "UPDATE users SET disabled_at=?, updated_at=? WHERE id=?",
            (now_iso() if disabled else None, now_iso(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _get_teacher_or_404(user_id: str) -> sqlite3.Row:
    user = get_user_by_id(user_id)
    if not user or user["role"] != USER_ROLE_TEACHER:
        raise HTTPException(404, "Teacher not found")
    return user
