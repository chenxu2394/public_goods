from __future__ import annotations

from fastapi import HTTPException

from ..db import db
from .._experiment import (
    build_phase_status,
    build_student_phase_report_conn,
    phase_computed_counts_conn,
    phase_for_round,
    phase_round_count_for_session,
    stage_of_session,
)
from .._sessions import get_session
from .types import StudentStatusPayload


def build_student_status_payload(session_id: str, student_id: str) -> StudentStatusPayload:
    sess = get_session(session_id)
    conn = db()
    stu = conn.execute(
        "SELECT * FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if not stu:
        conn.close()
        raise HTTPException(404, "student not found")

    cur_r = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_r)
    stage = stage_of_session(sess)
    total_rounds = int(sess["rounds"])
    computed_counts = phase_computed_counts_conn(conn, session_id)
    phase_statuses = build_phase_status(total_rounds, computed_counts)
    current_phase_report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), phase)

    cur_c = conn.execute(
        """
        SELECT contrib FROM contributions
        WHERE session_id=? AND round_no=? AND student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchone()

    submitted_actions_rows = conn.execute(
        """
        SELECT target_student_id, points
        FROM actions
        WHERE session_id=? AND round_no=? AND actor_student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchall()
    submitted_actions = {row["target_student_id"]: int(row["points"]) for row in submitted_actions_rows}

    group_view_rows = []
    action_targets = []
    group_view_visible = stage == "action" and phase in ("reward", "punishment") and stu["group_no"] is not None
    if group_view_visible:
        group_view_rows = conn.execute(
            """
            SELECT s.id, s.anonymous_id, COALESCE(c.contrib, 0) AS contrib
            FROM students s
            LEFT JOIN contributions c
                ON c.session_id=s.session_id
               AND c.round_no=?
               AND c.student_id=s.id
            WHERE s.session_id=? AND s.group_no=?
            ORDER BY s.group_pos ASC, s.joined_at ASC
        """,
            (cur_r, session_id, int(stu["group_no"])),
        ).fetchall()

        for row in group_view_rows:
            if row["id"] == stu["id"]:
                continue
            action_targets.append(
                {
                    "anonymous_id": row["anonymous_id"],
                    "points": submitted_actions.get(row["id"], 0),
                }
            )

    completed_phases = []
    for phase_status in phase_statuses:
        if not phase_status["completed"]:
            continue
        report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), str(phase_status["phase"]))
        if report is None:
            continue
        completed_phases.append(report)

    conn.close()

    current_phase_summary = {
        "phase": phase,
        "phase_label": phase.title(),
        "phase_round": phase_round,
        "computed_rounds": int(computed_counts.get(phase, 0)),
        "total_rounds": phase_round_count_for_session(total_rounds, phase),
        "latest_round": None,
        "latest_phase_round": None,
        "student_latest_income": 0.0,
        "student_latest_contrib": 0,
        "group_latest_income": 0.0,
        "group_latest_contrib": 0,
        "student_phase_cumulative": 0.0,
        "student_total_contrib": 0,
        "group_no": int(stu["group_no"]) if stu["group_no"] is not None else None,
        "group_phase_cumulative": 0.0,
        "group_total_contrib": 0,
    }
    if current_phase_report is not None:
        latest_student_round = current_phase_report["student_rows"][-1]
        latest_group_round = next(
            (
                row
                for row in current_phase_report["group_rows"]
                if row["round"] == latest_student_round["round"]
            ),
            None,
        )
        current_phase_summary.update(
            {
                "phase_label": current_phase_report["phase_label"],
                "latest_round": int(latest_student_round["round"]),
                "latest_phase_round": int(latest_student_round["phase_round"]),
                "student_latest_income": float(latest_student_round["income"]),
                "student_latest_contrib": int(latest_student_round["contrib"]),
                "group_latest_income": float(latest_group_round["group_income"]) if latest_group_round else 0.0,
                "group_latest_contrib": int(latest_group_round["group_total"]) if latest_group_round else 0,
                "student_phase_cumulative": float(current_phase_report["student_summary"]["final_phase_cumulative"]),
                "student_total_contrib": int(current_phase_report["student_summary"]["total_contrib"]),
                "group_no": int(current_phase_report["group_no"]),
                "group_phase_cumulative": float(current_phase_report["group_summary"]["total_income"]),
                "group_total_contrib": int(current_phase_report["group_summary"]["total_contrib"]),
            }
        )

    return {
        "session": {
            "id": sess["id"],
            "title": sess["title"],
            "group_size": int(sess["group_size"]),
            "multiplier": float(sess["multiplier"]),
            "endowment": int(sess["endowment"]),
            "rounds": int(sess["rounds"]),
            "locked": bool(int(sess["locked"])),
            "current_round": cur_r,
            "round_open": bool(int(sess["round_open"])),
            "action_open": bool(int(sess["action_open"])),
            "phase": phase,
            "phase_label": current_phase_summary["phase_label"],
            "phase_round": phase_round,
            "stage": stage,
        },
        "student": {
            "student_id": stu["student_id"],
            "name": stu["name"],
            "anonymous_id": stu["anonymous_id"],
            "group_no": stu["group_no"],
        },
        "phase_statuses": phase_statuses,
        "current_phase": current_phase_summary,
        "current_round": {
            "round": cur_r,
            "submitted_contrib": int(cur_c["contrib"]) if cur_c else None,
            "group_view_visible": group_view_visible,
        },
        "group_view": [
            {"anonymous_id": row["anonymous_id"], "contrib": int(row["contrib"])}
            for row in group_view_rows
            if row["id"] != stu["id"]
        ],
        "action_targets": action_targets,
        "completed_phases": completed_phases,
    }
