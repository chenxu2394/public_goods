from __future__ import annotations

import sqlite3

from fastapi import Request

from ...read_models import (
    build_admin_home_context,
    build_login_page_context,
    build_session_panel_context,
    build_share_link_page_context,
)
from ...views import _render_admin_home, _render_login_page, _render_session_panel, _render_share_link_page


def login_page_response(request: Request, *, status_code: int = 200, **extra: object):
    return _render_login_page(build_login_page_context(request, **extra), status_code=status_code)


def admin_home_response(
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


def session_panel_response(
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


def share_link_page_response(request: Request, sess: sqlite3.Row):
    return _render_share_link_page(build_share_link_page_context(request, sess))
