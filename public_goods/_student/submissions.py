from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from .._experiment import ensure_int
from .._sessions import get_session
from ..db import _run_write_with_retry, now_iso


def submit_student_contribution(session_id: str, student_id: str, contrib: str) -> dict[str, object]:
    sess = get_session(session_id)
    if int(sess["round_open"]) != 1 or int(sess["action_open"]) == 1:
        raise HTTPException(400, "Contribution stage is not open")

    parsed_contrib = ensure_int(contrib, 0, int(sess["endowment"]), "contrib")
    round_no = int(sess["current_round"])

    def _write_submit(conn: sqlite3.Connection) -> str:
        stu = conn.execute(
            "SELECT id FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
        if not stu:
            raise HTTPException(404, "student not found")

        sid = str(stu["id"])
        conn.execute(
            """
            INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(session_id, round_no, student_id)
            DO UPDATE SET contrib=excluded.contrib, created_at=excluded.created_at
        """,
            (session_id, round_no, sid, parsed_contrib, now_iso()),
        )
        return sid

    _run_write_with_retry(_write_submit)
    return {"ok": True, "round": round_no, "contrib": parsed_contrib}
