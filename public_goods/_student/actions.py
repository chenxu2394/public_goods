from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from ..config import MAX_ACTION_POINTS
from ..db import _run_write_with_retry, now_iso
from .._experiment import ensure_int, selected_phase
from .._sessions import get_session


def submit_student_actions(session_id: str, payload: object) -> dict[str, object]:
    sess = get_session(session_id)
    if int(sess["action_open"]) != 1:
        raise HTTPException(400, "Action stage is not open")

    round_no = int(sess["current_round"])
    phase = selected_phase(sess)
    if phase not in ("reward", "punishment"):
        raise HTTPException(400, "Current round has no reward/punishment stage")

    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")

    student_id = str(payload.get("student_id", "")).strip()
    allocations = payload.get("allocations")
    if not student_id:
        raise HTTPException(400, "student_id required")
    if not isinstance(allocations, dict):
        raise HTTPException(400, "allocations must be an object")

    def _write_actions(conn: sqlite3.Connection) -> int:
        stu = conn.execute(
            "SELECT id, group_no FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
        if not stu:
            raise HTTPException(404, "student not found")
        if stu["group_no"] is None:
            raise HTTPException(400, "group not assigned")

        actor_student_id = str(stu["id"])
        targets = conn.execute(
            """
            SELECT id, anonymous_id
            FROM students
            WHERE session_id=? AND group_no=? AND id<>?
            ORDER BY group_pos ASC, joined_at ASC
        """,
            (session_id, int(stu["group_no"]), actor_student_id),
        ).fetchall()

        target_by_anon = {target["anonymous_id"]: target["id"] for target in targets}

        rows_to_insert = []
        for anon_id, target_id in target_by_anon.items():
            raw_points = allocations.get(anon_id, 0)
            points = ensure_int(str(raw_points), 0, MAX_ACTION_POINTS, f"points[{anon_id}]")
            if points > 0:
                rows_to_insert.append((session_id, round_no, actor_student_id, target_id, points, now_iso()))

        conn.execute(
            "DELETE FROM actions WHERE session_id=? AND round_no=? AND actor_student_id=?",
            (session_id, round_no, actor_student_id),
        )

        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
                VALUES(?,?,?,?,?,?)
            """,
                rows_to_insert,
            )

        return len(rows_to_insert)

    targets_submitted = _run_write_with_retry(_write_actions)

    return {
        "ok": True,
        "round": round_no,
        "phase": phase,
        "targets_submitted": targets_submitted,
    }
