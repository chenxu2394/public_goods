from __future__ import annotations

import sqlite3
from typing import Dict, Optional, Tuple

from fastapi import HTTPException

from ..config import PHASE_LABELS, PHASE_ROUNDS, PHASES


def phase_for_round(round_no: int) -> Tuple[str, int]:
    if round_no <= PHASE_ROUNDS:
        return "baseline", round_no
    if round_no <= PHASE_ROUNDS * 2:
        return "reward", round_no - PHASE_ROUNDS
    return "punishment", max(1, round_no - PHASE_ROUNDS * 2)


def phase_start_round(phase: str) -> int:
    if phase == "baseline":
        return 1
    if phase == "reward":
        return PHASE_ROUNDS + 1
    if phase == "punishment":
        return PHASE_ROUNDS * 2 + 1
    raise HTTPException(400, "Invalid phase")


def phase_round_count_for_session(total_rounds: int, phase: str) -> int:
    start = phase_start_round(phase)
    if total_rounds < start:
        return 0
    return min(PHASE_ROUNDS, total_rounds - start + 1)


def phase_round_bounds_for_session(total_rounds: int, phase: str) -> Optional[Tuple[int, int]]:
    phase_rounds = phase_round_count_for_session(total_rounds, phase)
    if phase_rounds <= 0:
        return None
    start = phase_start_round(phase)
    return start, start + phase_rounds - 1


def stage_of_session(sess: sqlite3.Row) -> str:
    if int(sess["action_open"]) == 1:
        return "action"
    if int(sess["round_open"]) == 1:
        return "contribution"
    return "closed"


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase.title())


def selected_phase(sess: sqlite3.Row) -> Optional[str]:
    phase = sess["current_phase"]
    if phase in PHASES:
        return str(phase)
    # Compatibility guard for an older session that was already open when the
    # explicit per-round phase column was introduced.
    if int(sess["round_open"]) == 1 or int(sess["action_open"]) == 1:
        return phase_for_round(int(sess["current_round"]))[0]
    return None


def phase_context_conn(
    conn: sqlite3.Connection,
    sess: sqlite3.Row,
) -> Tuple[Optional[str], Optional[int]]:
    cur = int(sess["current_round"])
    completed = conn.execute(
        """
        SELECT phase, phase_round
        FROM results
        WHERE session_id=? AND round_no=?
        LIMIT 1
    """,
        (sess["id"], cur),
    ).fetchone()
    if completed is not None:
        return str(completed["phase"]), int(completed["phase_round"])

    phase = selected_phase(sess)
    if phase is None:
        return None, None
    previous_count = conn.execute(
        """
        SELECT COUNT(DISTINCT round_no) AS c
        FROM results
        WHERE session_id=? AND phase=? AND round_no<?
    """,
        (sess["id"], phase, cur),
    ).fetchone()["c"]
    return phase, int(previous_count or 0) + 1


def round_context(
    sess: sqlite3.Row,
    phase: Optional[str],
    phase_round: Optional[int],
    *,
    computed_rounds: int,
) -> Dict[str, object]:
    cur = int(sess["current_round"])
    stage = stage_of_session(sess)

    if stage == "contribution":
        if phase == "baseline":
            close_label = "Close contribution and compute round"
        else:
            close_label = f"Close contribution and open {phase_label(phase)} stage"
    elif stage == "action":
        close_label = (
            f"Close {phase_label(phase)} stage and compute round"
            if phase is not None
            else "Close action stage and compute round"
        )
    else:
        close_label = "Round is closed (open current round first)"

    return {
        "round": cur,
        "phase": phase,
        "phase_label": phase_label(phase) if phase is not None else "Not selected",
        "phase_round": phase_round,
        "stage": stage,
        "close_label": close_label,
        "completed": computed_rounds >= int(sess["rounds"]),
    }


def ensure_int(value: str, min_v: int, max_v: int, field: str) -> int:
    try:
        parsed = int(value)
    except Exception:
        raise HTTPException(400, f"{field} must be an integer")
    if parsed < min_v or parsed > max_v:
        raise HTTPException(400, f"{field} must be between {min_v} and {max_v}")
    return parsed


def next_round_after_compute(current_round: int, rounds: int) -> int:
    if current_round >= rounds:
        return rounds
    return current_round + 1
