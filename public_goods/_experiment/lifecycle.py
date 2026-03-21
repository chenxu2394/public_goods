from __future__ import annotations

from ..db import db
from .phases import next_round_after_compute


def open_round(session_id: str, round_no: int):
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=1, action_open=0 WHERE id=?",
        (round_no, session_id),
    )
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
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=0, action_open=0 WHERE id=?",
        (nxt, session_id),
    )
    conn.commit()
    conn.close()
