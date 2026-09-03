from __future__ import annotations

from fastapi import HTTPException

from ..config import PHASES
from ..db import db
from .phases import next_round_after_compute


def open_round(session_id: str, round_no: int, phase: str):
    phase = phase.strip().lower()
    if phase not in PHASES:
        raise HTTPException(400, "Invalid round type.")
    conn = db()
    updated = conn.execute(
        """
        UPDATE sessions
        SET current_phase=?, round_open=1, action_open=0
        WHERE id=?
          AND current_round=?
          AND round_open=0
          AND action_open=0
          AND NOT EXISTS(
              SELECT 1 FROM results WHERE session_id=? AND round_no=?
          )
    """,
        (phase, session_id, round_no, session_id, round_no),
    )
    if updated.rowcount != 1:
        conn.close()
        raise HTTPException(400, "This round is already open or has already been computed.")
    conn.commit()
    conn.close()


def close_round(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=0 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def open_action_stage(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=1 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def advance_round(session_id: str, current_round: int, rounds: int) -> None:
    nxt = next_round_after_compute(current_round, rounds)
    conn = db()
    if nxt == current_round:
        conn.execute(
            "UPDATE sessions SET round_open=0, action_open=0 WHERE id=?",
            (session_id,),
        )
    else:
        conn.execute(
            "UPDATE sessions SET current_round=?, current_phase=NULL, round_open=0, action_open=0 WHERE id=?",
            (nxt, session_id),
        )
    conn.commit()
    conn.close()
