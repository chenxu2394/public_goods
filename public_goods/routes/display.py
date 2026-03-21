from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..read_models import (
    build_display_status_payload,
    build_share_link_api_payload,
)
from ..sessions import get_session
from ..views import _render_share_link_page, templates
from .helpers import require_management_session


router = APIRouter()


@router.get("/display/{session_id}", response_class=HTMLResponse)
def display_page(request: Request, session_id: str):
    sess = get_session(session_id)
    return templates.TemplateResponse("display.html", {"request": request, "sess": sess})


@router.get("/api/{session_id}/display_status")
def api_display_status(session_id: str):
    return JSONResponse(build_display_status_payload(session_id))


@router.get("/api/admin/{session_id}/share_link")
def api_share_link_status(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate

    return JSONResponse(build_share_link_api_payload(sess))
