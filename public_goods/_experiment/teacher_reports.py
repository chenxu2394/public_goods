from __future__ import annotations

import sqlite3
from typing import Dict, List

from ..config import PHASES
from .phases import phase_label


def build_teacher_phase_reports_conn(
    conn: sqlite3.Connection,
    session_id: str,
    total_rounds: int,
    computed_counts: Dict[str, int],
) -> List[Dict[str, object]]:
    reports = []
    for phase in PHASES:
        if int(computed_counts.get(phase, 0)) <= 0:
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
                   MAX(
                       CASE WHEN r.round_no=(
                           SELECT MAX(r2.round_no)
                           FROM results r2
                           WHERE r2.session_id=r.session_id
                             AND r2.student_id=r.student_id
                             AND r2.phase=r.phase
                       ) THEN r.phase_cumulative END
                   ) AS final_phase_cumulative
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
                "computed_rounds": int(computed_counts.get(phase, 0)),
                "groups": groups,
                "students": students,
                "totals": {
                    "total_contrib": sum(row["total_contrib"] for row in groups),
                    "total_income": sum(row["total_income"] for row in groups),
                },
            }
        )
    return reports
