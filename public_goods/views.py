from __future__ import annotations

from typing import Mapping

from fastapi.templating import Jinja2Templates

from .config import TEMPLATE_DIR


templates = Jinja2Templates(directory=TEMPLATE_DIR)


def _render_login_page(context: Mapping[str, object], *, status_code: int = 200):
    return templates.TemplateResponse("admin_login.html", dict(context), status_code=status_code)


def _render_admin_home(context: Mapping[str, object], *, status_code: int = 200):
    return templates.TemplateResponse("admin_home.html", dict(context), status_code=status_code)


def _render_session_panel(context: Mapping[str, object], *, status_code: int = 200):
    return templates.TemplateResponse("session_panel.html", dict(context), status_code=status_code)


def _render_share_link_page(context: Mapping[str, object], *, status_code: int = 200):
    return templates.TemplateResponse("share_link.html", dict(context), status_code=status_code)
