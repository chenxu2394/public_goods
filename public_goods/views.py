from __future__ import annotations

import base64
import io
import sqlite3
from typing import Dict

import segno
from fastapi import Request
from fastapi.templating import Jinja2Templates

from .auth import _auth_configuration_error, _is_auth_configured
from .config import (
    DEMO_DEFAULT_STUDENT_COUNT,
    DEMO_MAX_STUDENT_COUNT,
    PUBLIC_BASE_URL,
    TEMPLATE_DIR,
    USER_ROLE_ADMIN,
)
from .db import db
from .experiment import (
    build_phase_status,
    build_teacher_phase_reports_conn,
    count_computed_rounds,
    current_round_contrib_rows_conn,
    current_round_progress_conn,
    phase_computed_counts_conn,
    phase_for_round,
    round_context,
    stage_of_session,
)
from .sessions import list_sessions, list_students, list_teachers, session_counts


templates = Jinja2Templates(directory=TEMPLATE_DIR)


def qr_svg_data_uri(content: str) -> str:
    qr = segno.make_qr(content, error="m")
    output = io.BytesIO()
    qr.save(output, kind="svg", scale=6, border=2, dark="#111111", light="#ffffff")
    svg = output.getvalue()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def _render_login_page(request: Request, *, status_code: int = 200, **extra: object):
    context = {
        "request": request,
        "configured": _is_auth_configured(),
        "configuration_error": _auth_configuration_error(),
        "pw_changed": request.query_params.get("pw_changed") == "1",
    }
    context.update(extra)
    return templates.TemplateResponse("admin_login.html", context, status_code=status_code)


def _build_admin_home_context(request: Request, user: sqlite3.Row, **extra: object) -> Dict[str, object]:
    is_admin = user["role"] == USER_ROLE_ADMIN
    must_change_password = int(user["must_change_password"]) == 1
    context: Dict[str, object] = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "must_change_password": must_change_password,
        "pw_change_required": must_change_password or request.query_params.get("pw_change_required") == "1",
        "sessions": list_sessions() if is_admin else list_sessions(str(user["id"])),
        "teachers": list_teachers() if is_admin else [],
    }
    context.update(extra)
    return context


def _render_admin_home(
    request: Request,
    user: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    return templates.TemplateResponse(
        "admin_home.html",
        _build_admin_home_context(request, user, **extra),
        status_code=status_code,
    )


def _share_link_context(sess: sqlite3.Row) -> Dict[str, str]:
    join_url = f"{PUBLIC_BASE_URL}/join/{sess['join_token']}"
    return {
        "join_url": join_url,
        "join_qr_data_uri": qr_svg_data_uri(join_url),
    }


def _render_session_panel(
    request: Request,
    user: sqlite3.Row,
    sess: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    students = list_students(str(sess["id"]))
    counts = session_counts(str(sess["id"]))
    conn = db()
    total_rounds = int(sess["rounds"])
    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)
    stage = stage_of_session(sess)
    computed_counts = phase_computed_counts_conn(conn, str(sess["id"]))
    phase_statuses = build_phase_status(total_rounds, computed_counts)
    round_progress = current_round_progress_conn(conn, str(sess["id"]), round_no)
    current_round_contrib_rows = (
        current_round_contrib_rows_conn(conn, str(sess["id"]), round_no)
        if stage == "action" and phase in ("reward", "punishment")
        else []
    )
    phase_reports = build_teacher_phase_reports_conn(conn, str(sess["id"]), total_rounds, computed_counts)
    conn.close()

    share_ctx = _share_link_context(sess)
    export_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/export"
    template_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/whitelist/template"
    display_url = f"{PUBLIC_BASE_URL}/display/{sess['id']}"
    share_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/share"

    context: Dict[str, object] = {
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
        "round_ctx": round_context(sess),
        "computed_rounds": count_computed_rounds(str(sess["id"])),
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
    return templates.TemplateResponse("session_panel.html", context, status_code=status_code)


def _render_share_link_page(request: Request, sess: sqlite3.Row):
    context: Dict[str, object] = {
        "request": request,
        "sess": sess,
        "share_api_url": f"{PUBLIC_BASE_URL}/api/admin/{sess['id']}/share_link",
    }
    context.update(_share_link_context(sess))
    return templates.TemplateResponse("share_link.html", context)
