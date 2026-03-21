from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from ...config import DEMO_DEFAULT_STUDENT_COUNT, DEMO_MAX_STUDENT_COUNT, MIN_GROUP_SIZE
from ...demo import (
    create_demo_class,
    simulate_demo_actions,
    simulate_demo_contributions,
    simulate_demo_current_phase,
    simulate_demo_current_round,
)
from ...experiment import ensure_int, open_action_stage as open_action_stage_for_session, open_round as open_round_for_session, phase_for_round, phase_label, stage_of_session
from ...sessions import get_session
from ..helpers import require_management_session
from .common import session_panel_response


router = APIRouter()


@router.post("/admin/{session_id}/demo/create_class")
def admin_demo_create_class(request: Request, session_id: str, student_count: str = Form(str(DEMO_DEFAULT_STUDENT_COUNT))):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        count = ensure_int(student_count, MIN_GROUP_SIZE, DEMO_MAX_STUDENT_COUNT, "student_count")
        create_demo_class(session_id, count)
    except HTTPException as exc:
        updated_sess = get_session(session_id)
        return session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=f"Created demo class with {count} mock students and locked the groups.",
    )


@router.post("/admin/{session_id}/demo/fill_contributions")
def admin_demo_fill_contributions(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        sess = get_session(session_id)
        if int(sess["demo_mode"]) != 1:
            raise HTTPException(400, "Enable demo mode on a fresh session before using demo automation.")
        if stage_of_session(sess) == "closed":
            open_round_for_session(session_id, int(sess["current_round"]))
            sess = get_session(session_id)
        filled = simulate_demo_contributions(session_id, int(sess["current_round"]))
    except HTTPException as exc:
        updated_sess = get_session(session_id)
        return session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=f"Auto-filled contribution choices for {filled} demo students in round {updated_sess['current_round']}.",
    )


@router.post("/admin/{session_id}/demo/fill_actions")
def admin_demo_fill_actions(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        sess = get_session(session_id)
        if int(sess["demo_mode"]) != 1:
            raise HTTPException(400, "Enable demo mode on a fresh session before using demo automation.")
        round_no = int(sess["current_round"])
        phase, _ = phase_for_round(round_no)
        if stage_of_session(sess) == "contribution" and phase in {"reward", "punishment"}:
            open_action_stage_for_session(session_id)
            sess = get_session(session_id)
        filled = simulate_demo_actions(session_id, round_no)
    except HTTPException as exc:
        updated_sess = get_session(session_id)
        return session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=f"Auto-filled {phase_label(phase)} actions for {filled} demo students in round {round_no}.",
    )


@router.post("/admin/{session_id}/demo/run_current_round")
def admin_demo_run_current_round(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        summary = simulate_demo_current_round(session_id)
    except HTTPException as exc:
        updated_sess = get_session(session_id)
        return session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=(
            f"Demo ran round {summary['round']} ({phase_label(str(summary['phase']))}) "
            f"with {summary['contrib_filled']} contribution auto-submissions and "
            f"{summary['actions_filled']} action auto-submissions."
        ),
    )


@router.post("/admin/{session_id}/demo/run_current_phase")
def admin_demo_run_current_phase(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        summary = simulate_demo_current_phase(session_id)
    except HTTPException as exc:
        updated_sess = get_session(session_id)
        return session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=(
            f"Demo autoplay completed {summary['phase_label']} with {summary['rounds_simulated']} auto-simulated rounds."
        ),
    )
