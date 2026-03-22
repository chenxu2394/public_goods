from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import DEFAULT_GROUP_SIZE, MAX_GROUP_SIZE, MIN_GROUP_SIZE, TOTAL_EXPERIMENT_ROUNDS, USER_ROLE_ADMIN, USER_ROLE_TEACHER
from ..._sessions import (
    _validate_session_title,
    archive_session_to_admin,
    create_session_record,
    delete_session,
    duplicate_session_as_admin,
    duplicate_session_setup_as_admin,
    get_session,
    get_user_by_id,
    set_session_title,
    transfer_session_owner,
)
from ..helpers import require_management_session, require_management_user
from .common import session_panel_response


router = APIRouter()


@router.post("/admin/create")
def admin_create_session(
    request: Request,
    title: str = Form(...),
    group_size: int = Form(DEFAULT_GROUP_SIZE),
    multiplier: float = Form(1.5),
    endowment: int = Form(10),
    rounds: int = Form(TOTAL_EXPERIMENT_ROUNDS),
):
    user, gate = require_management_user(request)
    if gate:
        return gate

    title = _validate_session_title(title)
    if group_size < MIN_GROUP_SIZE or group_size > MAX_GROUP_SIZE:
        raise HTTPException(400, f"group_size must be {MIN_GROUP_SIZE}..{MAX_GROUP_SIZE}")
    if multiplier <= 0 or multiplier > 10:
        raise HTTPException(400, "multiplier must be >0 and <=10")
    if endowment < 1 or endowment > 100:
        raise HTTPException(400, "endowment must be 1..100")
    if rounds < 1 or rounds > 50:
        raise HTTPException(400, "rounds must be 1..50")

    session_id = create_session_record(
        str(user["id"]),
        title,
        group_size=group_size,
        multiplier=multiplier,
        endowment=endowment,
        rounds=rounds,
    )
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.get("/admin/{session_id}", response_class=HTMLResponse)
def admin_panel(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate
    return session_panel_response(request, user, sess)


@router.post("/admin/{session_id}/title")
def admin_update_session_title(request: Request, session_id: str, title: str = Form(...)):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        title = _validate_session_title(title)
    except HTTPException as exc:
        return session_panel_response(request, user, sess, status_code=exc.status_code, title_error=exc.detail)

    set_session_title(session_id, title)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/transfer")
def admin_transfer_session(request: Request, session_id: str, teacher_user_id: str = Form(...)):
    user, sess, gate = require_management_session(request, session_id, admin_only=True)
    if gate:
        return gate

    teacher_user_id = teacher_user_id.strip()
    teacher = get_user_by_id(teacher_user_id)
    if not teacher or teacher["role"] != USER_ROLE_TEACHER:
        return session_panel_response(request, user, sess, status_code=400, transfer_error="Select a valid teacher.")
    if teacher["disabled_at"] is not None:
        return session_panel_response(
            request,
            user,
            sess,
            status_code=400,
            transfer_error="You cannot transfer a session to a disabled teacher.",
        )
    if sess["owner_user_id"] == teacher["id"]:
        return session_panel_response(
            request,
            user,
            sess,
            status_code=400,
            transfer_error=f"Session is already assigned to '{teacher['username']}'.",
        )

    transfer_session_owner(session_id, str(teacher["id"]))
    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        transfer_success=f"Session transferred to '{teacher['username']}'.",
    )


@router.post("/admin/{session_id}/duplicate")
def admin_duplicate_session(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id, admin_only=True)
    if gate:
        return gate

    duplicate_session_id = duplicate_session_as_admin(session_id)
    return RedirectResponse(url=f"/admin/{duplicate_session_id}", status_code=303)


@router.post("/admin/{session_id}/duplicate_setup")
def admin_duplicate_session_setup(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id, admin_only=True)
    if gate:
        return gate

    duplicate_session_id = duplicate_session_setup_as_admin(session_id)
    return RedirectResponse(url=f"/admin/{duplicate_session_id}", status_code=303)


@router.post("/admin/{session_id}/delete")
def admin_delete_session(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    if user["role"] == USER_ROLE_ADMIN:
        delete_session(session_id)
    else:
        archive_session_to_admin(session_id, str(user["id"]))
    return RedirectResponse(url="/admin", status_code=303)
