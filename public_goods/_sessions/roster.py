from __future__ import annotations

import csv
import io
import secrets
import sqlite3
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

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
    conn.close()
    return {"students": int(student_total), "whitelist": int(whitelist_total)}


def whitelist_template_csv() -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_id", "name"])
    writer.writerow(["20260001", "Alice"])
    writer.writerow(["20260002", "Bob"])
    return output.getvalue().encode("utf-8-sig")


def parse_whitelist_csv(file_bytes: bytes) -> List[Tuple[str, str]]:
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows: List[Tuple[str, str]] = []
    for i, row in enumerate(reader):
        if not row:
            continue
        if (
            i == 0
            and len(row) >= 2
            and row[0].strip().lower() == "student_id"
            and row[1].strip().lower() == "name"
        ):
            continue
        if len(row) < 2:
            continue
        student_id = row[0].strip()
        name = row[1].strip()
        if not student_id or not name:
            continue
        rows.append((student_id, name))
    return rows


def upsert_whitelist(session_id: str, entries: List[Tuple[str, str]]) -> Dict[str, int]:
    conn = db()
    cur = conn.cursor()
    added_at = now_iso()
    upserted = 0
    for student_id, name in entries:
        cur.execute(
            """
            INSERT INTO whitelist(session_id, student_id, name, added_at)
            VALUES(?,?,?,?)
            ON CONFLICT(session_id, student_id)
            DO UPDATE SET name=excluded.name, added_at=excluded.added_at
        """,
            (session_id, student_id, name, added_at),
        )
        upserted += 1
    conn.commit()
    conn.close()
    return {"upserted": upserted}


def clear_whitelist(session_id: str) -> None:
    conn = db()
    conn.execute("DELETE FROM whitelist WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def whitelist_check_or_raise(session_id: str, student_id: str, name: str) -> None:
    conn = db()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    if int(total) == 0:
        conn.close()
        raise HTTPException(403, "Whitelist not imported for this session. Please contact the instructor.")

    row = conn.execute(
        """
        SELECT name FROM whitelist
        WHERE session_id=? AND student_id=?
    """,
        (session_id, student_id),
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(403, "Student ID is not in the whitelist.")
    if str(row["name"]) != name:
        raise HTTPException(403, "Name does not match the whitelist exactly.")


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
