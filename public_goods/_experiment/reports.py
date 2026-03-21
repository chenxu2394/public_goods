from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from ..config import PHASES
from ..db import db
from .phases import phase_for_round, phase_label, phase_round_count_for_session


def count_computed_rounds(session_id: str) -> int:
    conn = db()
    computed = conn.execute(
        "SELECT COUNT(DISTINCT round_no) AS c FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    conn.close()
    return int(computed or 0)


def phase_computed_counts_conn(conn: sqlite3.Connection, session_id: str) -> Dict[str, int]:
    counts = {phase: 0 for phase in PHASES}
    rows = conn.execute(
        """
        SELECT round_no
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    for row in rows:
        phase, _ = phase_for_round(int(row["round_no"]))
        counts[phase] += 1
    return counts


def build_phase_status(total_rounds: int, computed_counts: Dict[str, int]) -> List[Dict[str, object]]:
    statuses = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0:
            continue
        statuses.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "total_rounds": phase_rounds,
                "computed_rounds": int(computed_counts.get(phase, 0)),
                "completed": int(computed_counts.get(phase, 0)) >= phase_rounds,
            }
        )
    return statuses


def build_student_phase_report_conn(
    conn: sqlite3.Connection,
    session_id: str,
    student_row_id: str,
    phase: str,
) -> Optional[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT round_no, phase_round, group_no, group_n, group_total, contrib,
               action_sent, action_received, action_cost, action_effect,
               income, phase_cumulative, computed_at
        FROM results
        WHERE session_id=? AND student_id=? AND phase=?
        ORDER BY round_no ASC
    """,
        (session_id, student_row_id, phase),
    ).fetchall()
    if not rows:
        return None

    group_no = int(rows[0]["group_no"])
    group_n = int(rows[0]["group_n"])
    group_round_rows = conn.execute(
        """
        SELECT round_no,
               MIN(phase_round) AS phase_round,
               MAX(group_total) AS group_total,
               SUM(income) AS group_income,
               AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=? AND phase=? AND group_no=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id, phase, group_no),
    ).fetchall()

    student_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "contrib": int(row["contrib"]),
            "action_sent": int(row["action_sent"]),
            "action_received": int(row["action_received"]),
            "action_cost": float(row["action_cost"]),
            "action_effect": float(row["action_effect"]),
            "income": float(row["income"]),
            "phase_cumulative": float(row["phase_cumulative"]),
            "computed_at": row["computed_at"],
        }
        for row in rows
    ]
    group_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "group_total": int(row["group_total"]),
            "group_income": float(row["group_income"]),
            "avg_contrib": float(row["avg_contrib"]),
        }
        for row in group_round_rows
    ]

    total_group_contrib = sum(row["group_total"] for row in group_rows)
    total_group_income = sum(row["group_income"] for row in group_rows)

    return {
        "phase": phase,
        "phase_label": phase_label(phase),
        "group_no": group_no,
        "group_n": group_n,
        "student_summary": {
            "total_contrib": sum(row["contrib"] for row in student_rows),
            "total_income": sum(row["income"] for row in student_rows),
            "action_sent": sum(row["action_sent"] for row in student_rows),
            "action_received": sum(row["action_received"] for row in student_rows),
            "final_phase_cumulative": student_rows[-1]["phase_cumulative"],
        },
        "student_rows": student_rows,
        "group_summary": {
            "total_contrib": total_group_contrib,
            "total_income": total_group_income,
            "avg_contrib": (total_group_contrib / (group_n * len(group_rows))) if group_rows and group_n > 0 else 0.0,
        },
        "group_rows": group_rows,
    }


def build_teacher_phase_reports_conn(
    conn: sqlite3.Connection,
    session_id: str,
    total_rounds: int,
    computed_counts: Dict[str, int],
) -> List[Dict[str, object]]:
    reports = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0 or int(computed_counts.get(phase, 0)) < phase_rounds:
            continue

        group_rows = conn.execute(
            """
            SELECT group_no,
                   MAX(group_n) AS group_n,
                   SUM(group_total) AS total_contrib,
                   SUM(group_income) AS total_income,
                   AVG(avg_contrib) AS avg_contrib
            FROM (
                SELECT group_no,
                       round_no,
                       MAX(group_n) AS group_n,
                       MAX(group_total) AS group_total,
                       SUM(income) AS group_income,
                       AVG(contrib) AS avg_contrib
                FROM results
                WHERE session_id=? AND phase=?
                GROUP BY group_no, round_no
            ) per_round
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, phase),
        ).fetchall()

        student_rows = conn.execute(
            """
            SELECT s.anonymous_id, s.student_id, s.name, r.group_no,
                   SUM(r.contrib) AS total_contrib,
                   SUM(r.income) AS total_income,
                   SUM(r.action_sent) AS action_sent,
                   SUM(r.action_received) AS action_received,
                   MAX(r.phase_cumulative) AS final_phase_cumulative
            FROM results r
            JOIN students s ON s.id=r.student_id
            WHERE r.session_id=? AND r.phase=?
            GROUP BY r.student_id, s.anonymous_id, s.student_id, s.name, r.group_no, s.group_pos, s.joined_at
            ORDER BY r.group_no ASC, s.group_pos ASC, s.joined_at ASC
        """,
            (session_id, phase),
        ).fetchall()

        groups = [
            {
                "group_no": int(row["group_no"]),
                "group_n": int(row["group_n"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "avg_contrib": float(row["avg_contrib"]),
            }
            for row in group_rows
        ]
        students = [
            {
                "anonymous_id": row["anonymous_id"],
                "student_id": row["student_id"],
                "name": row["name"],
                "group_no": int(row["group_no"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "action_sent": int(row["action_sent"]),
                "action_received": int(row["action_received"]),
                "final_phase_cumulative": float(row["final_phase_cumulative"]),
            }
            for row in student_rows
        ]

        reports.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "groups": groups,
                "students": students,
                "totals": {
                    "total_contrib": sum(row["total_contrib"] for row in groups),
                    "total_income": sum(row["total_income"] for row in groups),
                },
            }
        )
    return reports


def current_round_progress_conn(
    conn: sqlite3.Connection,
    session_id: str,
    round_no: int,
) -> Dict[str, object]:
    group_rows = conn.execute(
        """
        SELECT s.group_no,
               COUNT(DISTINCT s.id) AS student_total,
               COUNT(DISTINCT c.student_id) AS contrib_submitted,
               COUNT(DISTINCT a.actor_student_id) AS action_submitted
        FROM students s
        LEFT JOIN contributions c
          ON c.session_id=s.session_id
         AND c.round_no=?
         AND c.student_id=s.id
        LEFT JOIN actions a
          ON a.session_id=s.session_id
         AND a.round_no=?
         AND a.actor_student_id=s.id
        WHERE s.session_id=? AND s.group_no IS NOT NULL
        GROUP BY s.group_no
        ORDER BY s.group_no ASC
    """,
        (round_no, round_no, session_id),
    ).fetchall()

    groups = [
        {
            "group_no": int(row["group_no"]),
            "student_total": int(row["student_total"]),
            "contrib_submitted": int(row["contrib_submitted"] or 0),
            "action_submitted": int(row["action_submitted"] or 0),
        }
        for row in group_rows
    ]
    return {
        "student_total": sum(row["student_total"] for row in groups),
        "contrib_submitted": sum(row["contrib_submitted"] for row in groups),
        "action_submitted": sum(row["action_submitted"] for row in groups),
        "groups": groups,
    }


def current_round_contrib_rows_conn(
    conn: sqlite3.Connection,
    session_id: str,
    round_no: int,
) -> List[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT s.group_no, s.anonymous_id, s.student_id, s.name, COALESCE(c.contrib, 0) AS contrib
        FROM students s
        LEFT JOIN contributions c
          ON c.session_id=s.session_id
         AND c.round_no=?
         AND c.student_id=s.id
        WHERE s.session_id=? AND s.group_no IS NOT NULL
        ORDER BY s.group_no ASC, s.group_pos ASC, s.joined_at ASC
    """,
        (round_no, session_id),
    ).fetchall()
    return [
        {
            "group_no": int(row["group_no"]),
            "anonymous_id": row["anonymous_id"],
            "student_id": row["student_id"],
            "name": row["name"],
            "contrib": int(row["contrib"]),
        }
        for row in rows
    ]
