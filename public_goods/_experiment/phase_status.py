from __future__ import annotations

import sqlite3
from typing import Dict, List

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
