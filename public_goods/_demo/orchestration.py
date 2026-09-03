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
    phase_label,
    selected_phase,
    stage_of_session,
)
from .._sessions import get_session
from .actions import simulate_demo_actions
from .contributions import simulate_demo_contributions


def simulate_demo_current_round(session_id: str, phase: str | None = None) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Demo session must be grouped before auto-running rounds.")

    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    current_phase = selected_phase(sess)
    stage = stage_of_session(sess)

    if stage == "closed":
        current_phase = phase or current_phase
        if current_phase is None:
            raise HTTPException(400, "Choose a round type and open the round before running demo automation.")
        open_round(session_id, round_no, current_phase)
        stage = "contribution"
    if current_phase is None:
        raise HTTPException(400, "The current round does not have a selected type.")

    contrib_filled = 0
    if stage == "contribution":
        contrib_filled = simulate_demo_contributions(session_id, round_no)
        if current_phase == "baseline":
            close_round(session_id)
            compute_results(session_id, round_no)
            advance_round(session_id, round_no, rounds)
            return {
                "round": round_no,
                "phase": current_phase,
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
        "phase": current_phase,
        "contrib_filled": contrib_filled,
        "actions_filled": actions_filled,
    }


def simulate_demo_current_phase(session_id: str) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")

    rounds = int(sess["rounds"])
    current_phase = selected_phase(sess)
    if current_phase is None:
        raise HTTPException(400, "Choose a round type and open the round before autoplaying it.")

    conn = db()
    try:
        counts = phase_computed_counts_conn(conn, session_id)
    finally:
        conn.close()
    current_round = int(sess["current_round"])
    remaining_rounds = rounds - current_round + 1
    target_total = int(counts.get(current_phase, 0)) + remaining_rounds

    simulated_rounds = 0
    max_iterations = remaining_rounds + 1
    for _ in range(max_iterations):
        conn = db()
        try:
            counts = phase_computed_counts_conn(conn, session_id)
        finally:
            conn.close()
        if int(counts.get(current_phase, 0)) >= target_total:
            break
        simulate_demo_current_round(session_id, current_phase)
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
