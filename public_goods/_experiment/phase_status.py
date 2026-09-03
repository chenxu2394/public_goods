from __future__ import annotations

import sqlite3
from typing import Dict, List

from ..config import PHASES
from ..db import db
from .phases import phase_label


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
    phase_rows = conn.execute(
        """
        SELECT phase, COUNT(DISTINCT round_no) AS c
        FROM results
        WHERE session_id=?
        GROUP BY phase
    """,
        (session_id,),
    ).fetchall()
    for row in phase_rows:
        phase = str(row["phase"])
        if phase in counts:
            counts[phase] = int(row["c"] or 0)
    return counts


def build_phase_status(
    total_rounds: int,
    computed_counts: Dict[str, int],
    *,
    active_phase: str | None = None,
) -> List[Dict[str, object]]:
    statuses = []
    for phase in PHASES:
        statuses.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "total_rounds": None,
                "computed_rounds": int(computed_counts.get(phase, 0)),
                "completed": False,
                "active": phase == active_phase,
            }
        )
    return statuses
