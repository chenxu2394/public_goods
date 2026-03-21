from __future__ import annotations

import csv
import io
from typing import Tuple

from ..db import db
from ..experiment import phase_for_round


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
            default_phase, default_phase_round = phase_for_round(round_no)
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
                phase = default_phase
                phase_round = default_phase_round
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
