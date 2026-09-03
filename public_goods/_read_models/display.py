from __future__ import annotations

from ..db import db
from .._experiment import phase_context_conn, phase_label, stage_of_session
from .._sessions import get_session
from .types import DisplayStatusPayload


def build_display_status_payload(session_id: str) -> DisplayStatusPayload:
    sess = get_session(session_id)
    cur_round = int(sess["current_round"])
    stage = stage_of_session(sess)

    conn = db()
    phase, phase_round = phase_context_conn(conn, sess)
    latest_row = conn.execute(
        "SELECT MAX(round_no) AS r FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()
    latest_round = latest_row["r"]

    latest_groups = []
    if latest_round is not None:
        rows = conn.execute(
            """
            SELECT group_no, COUNT(*) AS group_n, SUM(contrib) AS group_total, AVG(contrib) AS avg_contrib
            FROM results
            WHERE session_id=? AND round_no=?
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, int(latest_round)),
        ).fetchall()
        latest_groups = [
            {
                "group_no": int(row["group_no"]),
                "group_n": int(row["group_n"]),
                "group_total": int(row["group_total"]),
                "avg_contrib": float(row["avg_contrib"]),
            }
            for row in rows
        ]

    series_rows = conn.execute(
        """
        SELECT round_no, AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    series = [{"round": int(row["round_no"]), "avg_contrib": float(row["avg_contrib"])} for row in series_rows]

    overall = conn.execute(
        "SELECT AVG(contrib) AS v FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["v"]
    conn.close()

    return {
        "session": {
            "id": sess["id"],
            "title": sess["title"],
            "rounds": int(sess["rounds"]),
            "current_round": cur_round,
            "phase": phase,
            "phase_label": phase_label(phase) if phase is not None else "Not selected",
            "phase_round": phase_round,
            "stage": stage,
        },
        "computed_rounds": len(series),
        "latest_computed_round": int(latest_round) if latest_round is not None else None,
        "latest_groups": latest_groups,
        "avg_series": series,
        "overall_avg_contrib": float(overall) if overall is not None else None,
    }
