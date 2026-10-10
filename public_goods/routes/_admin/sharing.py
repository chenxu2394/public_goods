from __future__ import annotations

import io

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ...read_models import build_export_csv
from ..._sessions import clear_whitelist, disable_session_join_link, get_session, parse_whitelist_csv, rotate_session_join_token, upsert_whitelist, whitelist_template_csv
from ..helpers import require_management_session
from .common import session_panel_response, share_link_page_response


router = APIRouter()


@router.get("/admin/{session_id}/share", response_class=HTMLResponse)
def admin_share_link_page(request: Request, session_id: str):
    user, sess, gate = require_management_session(request, session_id)
    if gate:
        return gate
    return share_link_page_response(request, sess)


@router.post("/admin/{session_id}/rotate_join_link")
def admin_rotate_join_link(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    rotate_session_join_token(session_id)
    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        join_link_success="Student join link refreshed. All previous join links no longer accept new joins.",
    )


@router.post("/admin/{session_id}/disable_join_link")
def admin_disable_join_link(request: Request, session_id: str):
    user, _, gate = require_management_session(request, session_id)
    if gate:
        return gate

    disable_session_join_link(session_id)
    updated_sess = get_session(session_id)
    return session_panel_response(
        request,
        user,
        updated_sess,
        join_link_success="Student join link disabled. No join links accept new joins.",
    )


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
