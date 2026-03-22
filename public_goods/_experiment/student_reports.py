from __future__ import annotations

import sqlite3
from typing import Dict, Optional

from .phases import phase_label


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
