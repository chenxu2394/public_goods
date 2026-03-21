from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ...config import PHASE_ROUNDS, PHASES
from ...db import db
from ...experiment import (
    advance_round,
    close_round as close_round_for_session,
    compute_results,
    lock_groups,
    open_action_stage as open_action_stage_for_session,
    open_round as open_round_for_session,
    phase_for_round,
    phase_label,
    phase_start_round,
    stage_of_session,
)
from ..helpers import require_management_session


router = APIRouter()


@router.post("/admin/{session_id}/lock")
def admin_lock(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    if int(sess["locked"]) == 0:
        lock_groups(session_id, int(sess["group_size"]))
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/switch_phase")
def admin_switch_phase(request: Request, session_id: str, phase: str = Form(...)):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    phase = phase.strip().lower()
    if phase not in PHASES:
        raise HTTPException(400, "invalid phase")

    rounds = int(sess["rounds"])
    start = phase_start_round(phase)
    if start > rounds:
        raise HTTPException(400, f"This session has only {rounds} rounds; phase {phase} is unavailable.")
    end = min(rounds, start + PHASE_ROUNDS - 1)

    conn = db()
    row = conn.execute(
        "SELECT MAX(round_no) AS r FROM results WHERE session_id=? AND round_no BETWEEN ? AND ?",
        (session_id, start, end),
    ).fetchone()
    max_done = row["r"]

    if max_done is None:
        target_round = start
    else:
        target_round = min(end, int(max_done) + 1)

    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=0, action_open=0 WHERE id=?",
        (target_round, session_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/open_round")
def admin_open_round(request: Request, session_id: str, round_no: int | None = Form(None)):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Please lock groups before opening rounds.")
    if stage_of_session(sess) != "closed":
        raise HTTPException(400, "Current round is already open.")

    if round_no is None:
        round_no = int(sess["current_round"])

    round_no = int(round_no)
    if round_no < 1 or round_no > int(sess["rounds"]):
        raise HTTPException(400, "invalid round")

    open_round_for_session(session_id, round_no)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/open_action_stage")
def admin_open_action_stage(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Please lock groups before opening rounds.")

    stage = stage_of_session(sess)
    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)

    if phase not in ("reward", "punishment"):
        raise HTTPException(400, "Baseline rounds do not have an action stage.")
    if stage == "closed":
        raise HTTPException(400, "Open the contribution stage first.")
    if stage == "action":
        raise HTTPException(400, f"{phase_label(phase)} stage is already open.")

    open_action_stage_for_session(session_id)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/close_and_compute")
def admin_close_and_compute(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    stage = stage_of_session(sess)
    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    phase, _ = phase_for_round(round_no)

    if stage == "closed":
        raise HTTPException(400, "Round is already closed. Open it first.")

    if phase in ("reward", "punishment") and stage != "action":
        raise HTTPException(400, f"Open the {phase_label(phase)} stage before computing this round.")

    close_round_for_session(session_id)
    compute_results(session_id, round_no)
    advance_round(session_id, round_no, rounds)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)
