from __future__ import annotations

import io
import sqlite3

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..auth import (
    _must_configure_auth,
    _validate_username,
    generate_temp_password,
    get_current_user,
    make_auth_token,
    verify_user_password,
)
from ..config import (
    ADMIN_COOKIE_SECURE,
    AUTH_COOKIE_NAME,
    AUTH_TOKEN_TTL_SECONDS,
    DEFAULT_GROUP_SIZE,
    DEMO_DEFAULT_STUDENT_COUNT,
    DEMO_MAX_STUDENT_COUNT,
    LEGACY_ADMIN_COOKIE_NAME,
    MAX_GROUP_SIZE,
    MIN_GROUP_SIZE,
    PHASE_ROUNDS,
    PHASES,
    TOTAL_EXPERIMENT_ROUNDS,
    USER_ROLE_ADMIN,
    USER_ROLE_TEACHER,
)
from ..db import db
from ..demo import (
    create_demo_class,
    simulate_demo_actions,
    simulate_demo_contributions,
    simulate_demo_current_phase,
    simulate_demo_current_round,
)
from ..experiment import (
    advance_round,
    close_round as close_round_for_session,
    compute_results,
    ensure_int,
    lock_groups,
    open_action_stage as open_action_stage_for_session,
    open_round as open_round_for_session,
    phase_for_round,
    phase_label,
    phase_start_round,
    stage_of_session,
)
from ..read_models import (
    build_admin_home_context,
    build_export_csv,
    build_login_page_context,
    build_session_panel_context,
    build_share_link_page_context,
)
from ..sessions import (
    _get_teacher_or_404,
    _rehash_legacy_user_password_conn,
    _validate_session_title,
    archive_session_to_admin,
    clear_whitelist,
    create_session_record,
    create_user,
    delete_session,
    duplicate_session_as_admin,
    duplicate_session_setup_as_admin,
    get_session,
    get_user_by_id,
    get_user_by_username,
    parse_whitelist_csv,
    rotate_session_join_token,
    set_session_title,
    set_user_disabled,
    set_user_password,
    transfer_session_owner,
    upsert_whitelist,
    whitelist_template_csv,
)
from ..views import _render_admin_home, _render_login_page, _render_session_panel, _render_share_link_page
from .helpers import (
    require_admin_user,
    require_authenticated_user,
    require_management_session,
    require_management_user,
)


router = APIRouter()


def _login_page_response(request: Request, *, status_code: int = 200, **extra: object):
    return _render_login_page(build_login_page_context(request, **extra), status_code=status_code)


def _admin_home_response(
    request: Request,
    user: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    return _render_admin_home(
        build_admin_home_context(request, user, **extra),
        status_code=status_code,
    )


def _session_panel_response(
    request: Request,
    user: sqlite3.Row,
    sess: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    return _render_session_panel(
        build_session_panel_context(request, user, sess, **extra),
        status_code=status_code,
    )


def _share_link_page_response(request: Request, sess: sqlite3.Row):
    return _render_share_link_page(build_share_link_page_context(request, sess))


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _login_page_response(request)


@router.post("/admin/login")
def admin_login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    _must_configure_auth()
    user = get_user_by_username(username)
    if not user:
        return _login_page_response(request, status_code=401, error="Incorrect username or password.")
    if user["disabled_at"] is not None:
        return _login_page_response(request, status_code=403, error="This account is disabled.")

    password_ok, needs_rehash = verify_user_password(user, password)
    if not password_ok:
        return _login_page_response(request, status_code=401, error="Incorrect username or password.")

    if needs_rehash:
        conn = db()
        try:
            _rehash_legacy_user_password_conn(conn, str(user["id"]), password)
            conn.commit()
        finally:
            conn.close()
        user = get_user_by_id(str(user["id"]))

    token = make_auth_token(user)
    redirect_url = "/admin?pw_change_required=1" if int(user["must_change_password"]) == 1 else "/admin"
    resp = RedirectResponse(url=redirect_url, status_code=303)
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=ADMIN_COOKIE_SECURE,
        samesite="lax",
        max_age=AUTH_TOKEN_TTL_SECONDS,
        path="/",
    )
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.post("/admin/change_password")
def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user, gate = require_authenticated_user(request)
    if gate:
        return gate

    def _render_error(error: str):
        return _admin_home_response(request, user, status_code=400, pw_error=error)

    password_ok, _ = verify_user_password(user, current_password)
    if not password_ok:
        return _render_error("Current password is incorrect.")
    if not new_password:
        return _render_error("New password must not be empty.")
    if new_password != confirm_password:
        return _render_error("New passwords do not match.")
    set_user_password(str(user["id"]), new_password, must_change_password=False)
    resp = RedirectResponse(url="/admin/login?pw_changed=1", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user, gate = require_authenticated_user(request)
    if gate:
        return gate
    return _admin_home_response(request, user)


@router.post("/admin/teachers")
def admin_create_teacher(request: Request, username: str = Form(...)):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    try:
        username = _validate_username(username)
        temp_password = generate_temp_password()
        create_user(
            username,
            USER_ROLE_TEACHER,
            temp_password,
            must_change_password=True,
        )
    except HTTPException as exc:
        return _admin_home_response(request, user, status_code=exc.status_code, teacher_error=exc.detail)
    except sqlite3.IntegrityError:
        return _admin_home_response(request, user, status_code=400, teacher_error="That username already exists.")

    return _admin_home_response(
        request,
        user,
        teacher_success=f"Created teacher '{username}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=username,
    )


@router.post("/admin/teachers/{user_id}/disable")
def admin_disable_teacher(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), True)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/teachers/{user_id}/enable")
def admin_enable_teacher(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), False)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/teachers/{user_id}/reset_password")
def admin_reset_teacher_password(request: Request, user_id: str):
    user, gate = require_admin_user(request)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    temp_password = generate_temp_password()
    set_user_password(str(teacher["id"]), temp_password, must_change_password=True)
    return _admin_home_response(
        request,
        user,
        teacher_success=f"Reset password for '{teacher['username']}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=str(teacher["username"]),
    )


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
    return _session_panel_response(request, user, sess)


@router.get("/admin/{session_id}/share", response_class=HTMLResponse)
def admin_share_link_page(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate
    return _share_link_page_response(request, sess)


@router.post("/admin/{session_id}/title")
def admin_update_session_title(request: Request, session_id: str, title: str = Form(...)):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    try:
        title = _validate_session_title(title)
    except HTTPException as exc:
        return _session_panel_response(request, user, sess, status_code=exc.status_code, title_error=exc.detail)

    set_session_title(session_id, title)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/rotate_join_link")
def admin_rotate_join_link(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    rotate_session_join_token(session_id)
    updated_sess = get_session(session_id)
    return _session_panel_response(
        request,
        user,
        updated_sess,
        join_link_success="Student join link refreshed. The previous join link no longer accepts new joins.",
    )


@router.post("/admin/{session_id}/transfer")
def admin_transfer_session(request: Request, session_id: str, teacher_user_id: str = Form(...)):
    user, sess, gate = require_management_session(request, session_id, admin_only=True)
    if gate:
        return gate

    teacher_user_id = teacher_user_id.strip()
    teacher = get_user_by_id(teacher_user_id)
    if not teacher or teacher["role"] != USER_ROLE_TEACHER:
        return _session_panel_response(request, user, sess, status_code=400, transfer_error="Select a valid teacher.")
    if teacher["disabled_at"] is not None:
        return _session_panel_response(
            request,
            user,
            sess,
            status_code=400,
            transfer_error="You cannot transfer a session to a disabled teacher.",
        )
    if sess["owner_user_id"] == teacher["id"]:
        return _session_panel_response(
            request,
            user,
            sess,
            status_code=400,
            transfer_error=f"Session is already assigned to '{teacher['username']}'.",
        )

    transfer_session_owner(session_id, str(teacher["id"]))
    updated_sess = get_session(session_id)
    return _session_panel_response(
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


@router.post("/admin/{session_id}/lock")
def admin_lock(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    if int(sess["locked"]) == 0:
        lock_groups(session_id, int(sess["group_size"]))
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


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
        return _session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return _session_panel_response(
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
        return _session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return _session_panel_response(
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
        return _session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return _session_panel_response(
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
        return _session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return _session_panel_response(
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
        return _session_panel_response(request, user, updated_sess, status_code=exc.status_code, demo_error=exc.detail)

    updated_sess = get_session(session_id)
    return _session_panel_response(
        request,
        user,
        updated_sess,
        demo_success=(
            f"Demo autoplay completed {summary['phase_label']} with {summary['rounds_simulated']} auto-simulated rounds."
        ),
    )


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


@router.get("/admin/{session_id}/export")
def admin_export(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    filename, data = build_export_csv(session_id, int(sess["rounds"]))
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/admin/{session_id}/whitelist/template")
def admin_whitelist_template(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    data = whitelist_template_csv()
    filename = f"whitelist_template_{session_id}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/admin/{session_id}/whitelist/upload")
async def admin_whitelist_upload(request: Request, session_id: str, file: UploadFile = File(...)):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    content = await file.read()
    try:
        entries = parse_whitelist_csv(content)
    except Exception:
        raise HTTPException(400, "Failed to parse CSV. Use UTF-8 CSV with columns: student_id,name")

    if len(entries) == 0:
        raise HTTPException(400, "CSV is empty or invalid. It must include student_id,name columns.")

    upsert_whitelist(session_id, entries)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@router.post("/admin/{session_id}/whitelist/clear")
def admin_whitelist_clear(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    clear_whitelist(session_id)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


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
