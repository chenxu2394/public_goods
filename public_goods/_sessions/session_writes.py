from __future__ import annotations

import secrets
import sqlite3

from fastapi import HTTPException

from ..config import TOTAL_EXPERIMENT_ROUNDS
from ..db import db, issue_join_token_conn, now_iso
from .bootstrap import _get_admin_user_conn


def _validate_session_title(title: str) -> str:
    value = title.strip()
    if not value:
        raise HTTPException(400, "Session title must not be empty.")
    if len(value) > 200:
        raise HTTPException(400, "Session title must be at most 200 characters.")
    return value


def create_session_record(
    owner_user_id: str,
    title: str,
    *,
    group_size: int,
    multiplier: float,
    endowment: int,
    rounds: int = TOTAL_EXPERIMENT_ROUNDS,
) -> str:
    session_id = secrets.token_urlsafe(6)
    conn = db()
    join_token = issue_join_token_conn(conn, reserved={session_id})
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, group_size, multiplier, endowment, rounds, created_at,
            locked, current_round, current_phase, round_open, action_open, join_token, owner_user_id
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            session_id,
            title,
            group_size,
            multiplier,
            endowment,
            rounds,
            now_iso(),
            0,
            1,
            None,
            0,
            0,
            join_token,
            owner_user_id,
        ),
    )
    conn.commit()
    conn.close()
    return session_id


def transfer_session_owner(session_id: str, owner_user_id: str) -> None:
    conn = db()
    try:
        conn.execute(
            """
            UPDATE sessions
            SET owner_user_id=?,
                teacher_removed_at=NULL,
                teacher_removed_by_user_id=NULL
            WHERE id=?
        """,
            (owner_user_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def archive_session_to_admin(session_id: str, removed_by_user_id: str) -> None:
    conn = db()
    try:
        admin_user = _get_admin_user_conn(conn)
        if not admin_user:
            raise HTTPException(500, "Admin account is unavailable.")
        conn.execute(
            """
            UPDATE sessions
            SET owner_user_id=?,
                teacher_removed_at=?,
                teacher_removed_by_user_id=?
            WHERE id=?
        """,
            (str(admin_user["id"]), now_iso(), removed_by_user_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def rotate_session_join_token(session_id: str) -> str:
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sess = conn.execute("SELECT join_token FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        new_join_token = issue_join_token_conn(
            conn, reserved={str(sess["join_token"] or ""), str(session_id)}
        )
        conn.execute(
            "UPDATE sessions SET join_token=?, join_link_enabled=1 WHERE id=?",
            (new_join_token, session_id),
        )
        conn.commit()
        return new_join_token
    finally:
        conn.close()


def disable_session_join_link(session_id: str) -> None:
    conn = db()
    try:
        conn.execute("UPDATE sessions SET join_link_enabled=0 WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def set_session_title(session_id: str, title: str) -> None:
    conn = db()
    try:
        conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
        conn.commit()
    finally:
        conn.close()


def delete_session(session_id: str) -> None:
    conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM actions WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM contributions WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM results WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM students WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM whitelist WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
