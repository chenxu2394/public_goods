from __future__ import annotations

import csv
import io
from typing import Tuple

from ..db import db
def build_export_csv(session_id: str, rounds: int) -> Tuple[str, bytes]:
    conn = db()
    students = conn.execute(
        """
        SELECT id, anonymous_id, student_id, name, group_no
        FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()

    contrib_rows = conn.execute(
        "SELECT round_no, student_id, contrib FROM contributions WHERE session_id=?",
        (session_id,),
    ).fetchall()
    contrib_map = {(int(row["round_no"]), row["student_id"]): int(row["contrib"]) for row in contrib_rows}

    result_rows = conn.execute(
        """
        SELECT round_no, student_id, phase, phase_round, income, cumulative, phase_cumulative, action_sent, action_received
        FROM results
        WHERE session_id=?
    """,
        (session_id,),
    ).fetchall()
    result_map = {(int(row["round_no"]), row["student_id"]): row for row in result_rows}
    sess = conn.execute(
        "SELECT current_round, current_phase FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    current_round = int(sess["current_round"]) if sess is not None else None
    current_phase = str(sess["current_phase"]) if sess is not None and sess["current_phase"] else None
    current_phase_round = None
    if current_phase is not None and current_round is not None:
        row = conn.execute(
            "SELECT COUNT(DISTINCT round_no) AS c FROM results WHERE session_id=? AND phase=? AND round_no<?",
            (session_id, current_phase, current_round),
        ).fetchone()
        current_phase_round = int(row["c"] or 0) + 1
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "experiment_id",
            "anonymous_id",
            "student_id",
            "name",
            "phase",
            "phase_round",
            "round_no",
            "contribution",
            "income",
            "cumulative",
            "phase_cumulative",
            "action_sent",
            "action_received",
        ]
    )

    for student in students:
        for round_no in range(1, rounds + 1):
            contrib = contrib_map.get((round_no, student["id"]), "")
            result_row = result_map.get((round_no, student["id"]))
            if result_row:
                phase = result_row["phase"]
                phase_round = result_row["phase_round"]
                income = result_row["income"]
                cumulative = result_row["cumulative"]
                phase_cumulative = result_row["phase_cumulative"]
                action_sent = result_row["action_sent"]
                action_received = result_row["action_received"]
            else:
                phase = current_phase if round_no == current_round else ""
                phase_round = current_phase_round if round_no == current_round else ""
                income = ""
                cumulative = ""
                phase_cumulative = ""
                action_sent = ""
                action_received = ""

            writer.writerow(
                [
                    session_id,
                    student["anonymous_id"],
                    student["student_id"],
                    student["name"],
                    phase,
                    phase_round,
                    round_no,
                    contrib,
                    income,
                    cumulative,
                    phase_cumulative,
                    action_sent,
                    action_received,
                ]
            )

    data = output.getvalue().encode("utf-8-sig")
    filename = f"public_goods_{session_id}.csv"
    return filename, data
