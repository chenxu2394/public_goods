from __future__ import annotations

import secrets
import sqlite3
from typing import Dict, List, Optional

from ..db import _generate_anonymous_id, _run_write_with_retry, db, now_iso


def list_students(session_id: str) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()
    conn.close()
    return rows


def get_student_by_public_id(session_id: str, student_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute(
            "SELECT * FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
    finally:
        conn.close()


def session_counts(session_id: str) -> Dict[str, int]:
    conn = db()
    student_total = conn.execute(
        "SELECT COUNT(*) AS c FROM students WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    whitelist_total = conn.execute(
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    roster_joined = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM whitelist w
        JOIN students s ON s.session_id=w.session_id AND s.student_id=w.student_id
        WHERE w.session_id=?
        """,
        (session_id,),
    ).fetchone()["c"]
    conn.close()
    return {
        "students": int(student_total),
        "whitelist": int(whitelist_total),
        "roster_joined": int(roster_joined),
    }


def upsert_joined_student(session_id: str, student_id: str, name: str) -> str:
    def _write_join(conn: sqlite3.Connection) -> str:
        existing = conn.execute(
            "SELECT id, anonymous_id FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()

        if existing:
            sid = str(existing["id"])
            if existing["anonymous_id"]:
                conn.execute("UPDATE students SET name=? WHERE id=?", (name, sid))
            else:
                used_ids = {
                    row["anonymous_id"]
                    for row in conn.execute(
                        "SELECT anonymous_id FROM students WHERE session_id=?",
                        (session_id,),
                    ).fetchall()
                    if row["anonymous_id"]
                }
                anonymous_id = _generate_anonymous_id(used_ids)
                conn.execute("UPDATE students SET name=?, anonymous_id=? WHERE id=?", (name, anonymous_id, sid))
            return sid

        used_ids = {
            row["anonymous_id"]
            for row in conn.execute("SELECT anonymous_id FROM students WHERE session_id=?", (session_id,)).fetchall()
            if row["anonymous_id"]
        }
        anonymous_id = _generate_anonymous_id(used_ids)
        sid = secrets.token_urlsafe(8)
        conn.execute(
            """
            INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            (sid, session_id, student_id, name, anonymous_id, now_iso(), None, None),
        )
        return sid

    return _run_write_with_retry(_write_join)

