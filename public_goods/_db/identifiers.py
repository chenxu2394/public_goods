from __future__ import annotations

import secrets
import sqlite3
from typing import Optional


def _generate_anonymous_id(existing: set[str]) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"

    for _ in range(1000):
        code = f"{secrets.choice(letters)}{secrets.choice(digits)}"
        if code not in existing:
            return code

    while True:
        code = f"{secrets.choice(letters)}{secrets.choice(digits)}{secrets.choice(letters)}"
        if code not in existing:
            return code


def _backfill_anonymous_ids(conn: sqlite3.Connection) -> None:
    sessions = conn.execute("SELECT DISTINCT session_id FROM students").fetchall()
    for session_row in sessions:
        session_id = session_row["session_id"]
        students = conn.execute(
            "SELECT id, anonymous_id FROM students WHERE session_id=? ORDER BY joined_at ASC",
            (session_id,),
        ).fetchall()
        used = {s["anonymous_id"] for s in students if s["anonymous_id"]}
        updates = []
        for student in students:
            if student["anonymous_id"]:
                continue
            anonymous_id = _generate_anonymous_id(used)
            used.add(anonymous_id)
            updates.append((anonymous_id, student["id"]))
        if updates:
            conn.executemany("UPDATE students SET anonymous_id=? WHERE id=?", updates)


def _generate_unique_token_conn(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    nbytes: int,
    reserved: Optional[set[str]] = None,
) -> str:
    reserved = reserved or set()
    for _ in range(100):
        token = secrets.token_urlsafe(nbytes)
        if token in reserved:
            continue
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (token,)).fetchone()
        if row is None:
            return token
    raise RuntimeError(f"Failed to generate a unique token for {table}.{column}")


def issue_join_token_conn(conn: sqlite3.Connection, *, reserved: Optional[set[str]] = None) -> str:
    """Reserve a token permanently so refreshing cannot reactivate an old link."""
    reserved = reserved or set()
    for _ in range(100):
        token = secrets.token_urlsafe(12)
        if token in reserved:
            continue
        if conn.execute("SELECT 1 FROM sessions WHERE join_token=?", (token,)).fetchone():
            continue
        cursor = conn.execute("INSERT OR IGNORE INTO issued_join_tokens(token) VALUES(?)", (token,))
        if cursor.rowcount == 1:
            return token
    raise RuntimeError("Failed to issue a unique join token")

