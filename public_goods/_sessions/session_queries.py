from __future__ import annotations

import sqlite3
from typing import List, Optional

from fastapi import HTTPException

from ..config import USER_ROLE_ADMIN
from ..db import db


def get_session(session_id: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute(
        """
        SELECT s.*,
               owner.username AS owner_username,
               removed_by.username AS teacher_removed_by_username
        FROM sessions s
        LEFT JOIN users owner ON owner.id=s.owner_user_id
        LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
        WHERE s.id=?
    """,
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def get_session_by_join_token(join_token: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute(
        """
        SELECT s.*,
               owner.username AS owner_username,
               removed_by.username AS teacher_removed_by_username
        FROM sessions s
        LEFT JOIN users owner ON owner.id=s.owner_user_id
        LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
        WHERE s.join_token=? AND s.join_link_enabled=1
    """,
        (join_token,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Join link is invalid or expired.")
    return row


def get_session_for_user(session_id: str, user: sqlite3.Row) -> sqlite3.Row:
    sess = get_session(session_id)
    if user["role"] != USER_ROLE_ADMIN and sess["owner_user_id"] != user["id"]:
        raise HTTPException(404, "Session not found")
    return sess


def list_sessions(owner_user_id: Optional[str] = None) -> List[sqlite3.Row]:
    conn = db()
    try:
        if owner_user_id is None:
            rows = conn.execute(
                """
                SELECT s.*,
                       owner.username AS owner_username,
                       removed_by.username AS teacher_removed_by_username
                FROM sessions s
                LEFT JOIN users owner ON owner.id=s.owner_user_id
                LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
                ORDER BY s.created_at DESC
            """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.*,
                       owner.username AS owner_username,
                       removed_by.username AS teacher_removed_by_username
                FROM sessions s
                LEFT JOIN users owner ON owner.id=s.owner_user_id
                LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
                WHERE s.owner_user_id=?
                ORDER BY s.created_at DESC
            """,
                (owner_user_id,),
            ).fetchall()
        return rows
    finally:
        conn.close()

