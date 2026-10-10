from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .._student import (
    submit_student_actions,
    submit_student_contribution,
    submit_student_join,
)
from ..read_models import build_student_status_payload
from .._sessions import (
    get_session,
    get_session_by_join_token,
    get_student_by_public_id,
    session_counts,
)
from ..views import templates


router = APIRouter()


def _join_link_unavailable(request: Request):
    return templates.TemplateResponse(
        request,
        "join_unavailable.html",
        {"request": request},
        status_code=404,
    )


@router.get("/join/{join_token}", response_class=HTMLResponse)
def join_page(request: Request, join_token: str):
    try:
        sess = get_session_by_join_token(join_token)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return _join_link_unavailable(request)
    session_id = str(sess["id"])
    counts = session_counts(session_id)
    return templates.TemplateResponse(
        request,
        "join.html",
        {"request": request, "sess": sess, "counts": counts, "join_token": join_token},
    )


@router.post("/join/{join_token}")
def join_submit(request: Request, join_token: str, student_id: str = Form(...), name: str = Form(...)):
    try:
        session_id, student_id = submit_student_join(join_token, student_id, name)
    except HTTPException as exc:
        if exc.status_code == 404:
            return _join_link_unavailable(request)
        if exc.status_code not in (400, 403):
            raise
        try:
            sess = get_session_by_join_token(join_token)
        except HTTPException as link_exc:
            if link_exc.status_code != 404:
                raise
            return _join_link_unavailable(request)
        return templates.TemplateResponse(
            request,
            "join.html",
            {
                "request": request,
                "sess": sess,
                "counts": session_counts(str(sess["id"])),
                "join_token": join_token,
                "error": exc.detail,
                "student_id": student_id,
                "name": name,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(url=f"/s/{session_id}/{student_id}", status_code=303)


@router.get("/s/{session_id}/{student_id}", response_class=HTMLResponse)
def student_page(request: Request, session_id: str, student_id: str):
    sess = get_session(session_id)
    stu = get_student_by_public_id(session_id, student_id)
    if not stu:
        return RedirectResponse(url=f"/join/{session_id}", status_code=303)
    return templates.TemplateResponse(request, "student.html", {"request": request, "sess": sess, "stu": stu})


@router.post("/api/{session_id}/submit")
def api_submit(session_id: str, student_id: str = Form(...), contrib: str = Form(...)):
    return submit_student_contribution(session_id, student_id, contrib)


@router.post("/api/{session_id}/submit_actions")
async def api_submit_actions(session_id: str, request: Request):
    payload = await request.json()
    return await run_in_threadpool(submit_student_actions, session_id, payload)


@router.get("/api/{session_id}/status")
def api_status(session_id: str, student_id: str):
    return JSONResponse(build_student_status_payload(session_id, student_id))
