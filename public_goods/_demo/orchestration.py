from __future__ import annotations

from typing import Dict

from fastapi import HTTPException

from ..db import db
from .._experiment import (
    advance_round,
    close_round,
    compute_results,
    open_action_stage,
    open_round,
    phase_computed_counts_conn,
    phase_for_round,
    phase_label,
    phase_round_bounds_for_session,
    phase_round_count_for_session,
    stage_of_session,
)
from .._sessions import get_session
from .actions import simulate_demo_actions
from .contributions import simulate_demo_contributions


def simulate_demo_current_round(session_id: str) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Demo session must be grouped before auto-running rounds.")

    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    phase, _ = phase_for_round(round_no)
    stage = stage_of_session(sess)

    if stage == "closed":
        open_round(session_id, round_no)
        stage = "contribution"

    contrib_filled = 0
    if stage == "contribution":
        contrib_filled = simulate_demo_contributions(session_id, round_no)
        if phase == "baseline":
            close_round(session_id)
            compute_results(session_id, round_no)
            advance_round(session_id, round_no, rounds)
            return {
                "round": round_no,
                "phase": phase,
                "contrib_filled": contrib_filled,
                "actions_filled": 0,
            }
        open_action_stage(session_id)
        stage = "action"

    if stage != "action":
        raise HTTPException(400, "Unable to advance the session automatically from the current stage.")

    actions_filled = simulate_demo_actions(session_id, round_no)
    close_round(session_id)
    compute_results(session_id, round_no)
    advance_round(session_id, round_no, rounds)
    return {
        "round": round_no,
        "phase": phase,
        "contrib_filled": contrib_filled,
        "actions_filled": actions_filled,
    }


def simulate_demo_current_phase(session_id: str) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")

    rounds = int(sess["rounds"])
    current_phase, _ = phase_for_round(int(sess["current_round"]))
    phase_bounds = phase_round_bounds_for_session(rounds, current_phase)
    if phase_bounds is None:
        raise HTTPException(400, "Current phase is unavailable for this session.")

    conn = db()
    try:
        counts = phase_computed_counts_conn(conn, session_id)
    finally:
        conn.close()
    target_total = phase_round_count_for_session(rounds, current_phase)

    simulated_rounds = 0
    max_iterations = target_total + 2
    for _ in range(max_iterations):
        conn = db()
        try:
            counts = phase_computed_counts_conn(conn, session_id)
        finally:
            conn.close()
        if int(counts.get(current_phase, 0)) >= target_total:
            break
        simulate_demo_current_round(session_id)
        simulated_rounds += 1

    conn = db()
    try:
        counts = phase_computed_counts_conn(conn, session_id)
    finally:
        conn.close()
    if int(counts.get(current_phase, 0)) < target_total:
        raise HTTPException(500, "Demo autoplay did not finish the current phase.")

    return {
        "phase": current_phase,
        "phase_label": phase_label(current_phase),
        "rounds_simulated": simulated_rounds,
    }
