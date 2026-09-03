from __future__ import annotations

import sqlite3

from fastapi import Request

from ..auth import _auth_configuration_error, _is_auth_configured
from ..config import (
    AUTH_MODE,
    AUTH_MODE_EASY_AUTH,
    DEMO_DEFAULT_STUDENT_COUNT,
    DEMO_MAX_STUDENT_COUNT,
    PUBLIC_BASE_URL,
    USER_ROLE_ADMIN,
)
from ..db import db
from .._experiment import (
    build_phase_status,
    build_teacher_phase_reports_conn,
    current_round_contrib_rows_conn,
    current_round_progress_conn,
    phase_context_conn,
    phase_computed_counts_conn,
    round_context,
    stage_of_session,
)
from .._sessions import list_sessions, list_students, list_teachers, session_counts
from .share import build_share_link_context
from .types import AdminHomeContext, LoginPageContext, SessionPanelContext


def build_login_page_context(request: Request, **extra: object) -> LoginPageContext:
    context: LoginPageContext = {
        "request": request,
        "configured": _is_auth_configured(),
        "configuration_error": _auth_configuration_error(),
        "pw_changed": request.query_params.get("pw_changed") == "1",
        "easy_auth": AUTH_MODE == AUTH_MODE_EASY_AUTH,
    }
    context.update(extra)
    return context


def build_admin_home_context(request: Request, user: sqlite3.Row, **extra: object) -> AdminHomeContext:
    is_admin = user["role"] == USER_ROLE_ADMIN
    easy_auth = AUTH_MODE == AUTH_MODE_EASY_AUTH
    must_change_password = not easy_auth and int(user["must_change_password"]) == 1
    context: AdminHomeContext = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "must_change_password": must_change_password,
        "pw_change_required": must_change_password or request.query_params.get("pw_change_required") == "1",
        "easy_auth": easy_auth,
        "sessions": list_sessions() if is_admin else list_sessions(str(user["id"])),
        "teachers": list_teachers() if is_admin else [],
    }
    context.update(extra)
    return context


def build_session_panel_context(
    request: Request,
    user: sqlite3.Row,
    sess: sqlite3.Row,
    **extra: object,
) -> SessionPanelContext:
    students = list_students(str(sess["id"]))
    counts = session_counts(str(sess["id"]))
    conn = db()
    total_rounds = int(sess["rounds"])
    round_no = int(sess["current_round"])
    stage = stage_of_session(sess)
    computed_counts = phase_computed_counts_conn(conn, str(sess["id"]))
    computed_rounds = sum(computed_counts.values())
    phase, phase_round = phase_context_conn(conn, sess)
    phase_statuses = build_phase_status(
        total_rounds,
        computed_counts,
        active_phase=phase if computed_rounds < total_rounds else None,
    )
    round_progress = current_round_progress_conn(conn, str(sess["id"]), round_no)
    current_round_contrib_rows = (
        current_round_contrib_rows_conn(conn, str(sess["id"]), round_no)
        if stage == "action" and phase in ("reward", "punishment")
        else []
    )
    phase_reports = build_teacher_phase_reports_conn(conn, str(sess["id"]), total_rounds, computed_counts)
    conn.close()

    share_ctx = build_share_link_context(sess)
    export_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/export"
    template_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/whitelist/template"
    display_url = f"{PUBLIC_BASE_URL}/display/{sess['id']}"
    share_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/share"

    context: SessionPanelContext = {
        "request": request,
        "user": user,
        "is_admin": user["role"] == USER_ROLE_ADMIN,
        "sess": sess,
        "students": students,
        "counts": counts,
        "join_url": share_ctx["join_url"],
        "join_qr_data_uri": share_ctx["join_qr_data_uri"],
        "share_url": share_url,
        "export_url": export_url,
        "template_url": template_url,
        "display_url": display_url,
        "round_ctx": round_context(
            sess,
            phase,
            phase_round,
            computed_rounds=computed_rounds,
        ),
        "computed_rounds": computed_rounds,
        "phase_statuses": phase_statuses,
        "round_progress": round_progress,
        "current_round_contrib_rows": current_round_contrib_rows,
        "phase_reports": phase_reports,
        "demo_default_student_count": min(
            DEMO_MAX_STUDENT_COUNT,
            max(DEMO_DEFAULT_STUDENT_COUNT, int(sess["group_size"]) * 4),
        ),
        "transfer_teachers": list_teachers(active_only=True) if user["role"] == USER_ROLE_ADMIN else [],
    }
    context.update(extra)
    return context
