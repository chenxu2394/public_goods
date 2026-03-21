from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import HTTPException

from ..db import db, now_iso
from ..experiment import phase_for_round
from .utils import _demo_profile_for_student, _stable_int


def _demo_previous_result_conn(
    conn: sqlite3.Connection,
    session_id: str,
    student_row_id: str,
    round_no: int,
) -> Optional[sqlite3.Row]:
    if round_no <= 1:
        return None
    return conn.execute(
        """
        SELECT group_total, group_n, action_received
        FROM results
        WHERE session_id=? AND student_id=? AND round_no=?
    """,
        (session_id, student_row_id, round_no - 1),
    ).fetchone()


def _demo_contribution_for_student(
    student_public_id: str,
    endowment: int,
    phase: str,
    round_no: int,
    previous_result: Optional[sqlite3.Row],
) -> int:
    profile = _demo_profile_for_student(student_public_id)
    base_by_profile = {
        "cooperator": round(endowment * 0.8),
        "conditional": round(endowment * 0.5),
        "reciprocator": round(endowment * 0.6),
        "free_rider": round(endowment * 0.2),
    }
    target = int(base_by_profile[profile])

    if phase == "reward":
        if profile in {"cooperator", "reciprocator"}:
            target += 1
    elif phase == "punishment":
        if profile == "free_rider":
            target += 1

    if previous_result is not None:
        group_avg = float(previous_result["group_total"]) / max(1, int(previous_result["group_n"]))
        received = int(previous_result["action_received"] or 0)
        if profile == "conditional":
            target = round(group_avg)
        elif profile == "reciprocator":
            target = round((target + group_avg) / 2)
            if received > 0:
                target += 1 if phase == "reward" else 2
        elif profile == "free_rider":
            if phase == "punishment" and received > 0:
                target += min(3, received)
            elif phase == "reward" and received > 0:
                target += 1
        else:
            target = max(target, round(group_avg))

    noise = (_stable_int(f"{student_public_id}:{round_no}") % 3) - 1
    return max(0, min(endowment, int(target + noise)))


def simulate_demo_contributions(session_id: str, round_no: int) -> int:
    conn = db()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        if int(sess["demo_mode"]) != 1:
            raise HTTPException(400, "Demo automation is available only for demo sessions.")
        if int(sess["locked"]) != 1:
            raise HTTPException(400, "Demo session must be grouped before auto-submitting contributions.")
        if int(sess["round_open"]) != 1 or int(sess["action_open"]) == 1:
            raise HTTPException(400, "Contribution stage must be open before auto-submitting contributions.")

        phase, _ = phase_for_round(round_no)
        endowment = int(sess["endowment"])
        students = conn.execute(
            """
            SELECT id, student_id
            FROM students
            WHERE session_id=? AND group_no IS NOT NULL
            ORDER BY group_no ASC, group_pos ASC, joined_at ASC
        """,
            (session_id,),
        ).fetchall()
        existing = {
            row["student_id"]
            for row in conn.execute(
                "SELECT student_id FROM contributions WHERE session_id=? AND round_no=?",
                (session_id, round_no),
            ).fetchall()
        }

        rows_to_insert = []
        for student in students:
            if student["id"] in existing:
                continue
            previous_result = _demo_previous_result_conn(conn, session_id, str(student["id"]), round_no)
            contrib = _demo_contribution_for_student(
                str(student["student_id"]),
                endowment,
                phase,
                round_no,
                previous_result,
            )
            rows_to_insert.append((session_id, round_no, student["id"], contrib, now_iso()))

        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(session_id, round_no, student_id)
                DO UPDATE SET contrib=excluded.contrib, created_at=excluded.created_at
            """,
                rows_to_insert,
            )
            conn.commit()
        return len(rows_to_insert)
    finally:
        conn.close()
