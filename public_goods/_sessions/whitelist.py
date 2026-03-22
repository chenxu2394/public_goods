from __future__ import annotations

import csv
import io
from typing import Dict, List, Tuple

from fastapi import HTTPException

from ..db import db, now_iso


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

