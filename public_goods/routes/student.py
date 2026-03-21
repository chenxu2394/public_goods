from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import MAX_ACTION_POINTS
from ..db import _run_write_with_retry, now_iso
from ..experiment import assign_late_joiner, ensure_int, phase_for_round
from ..read_models import build_student_status_payload
from ..sessions import (
    get_session,
    get_session_by_join_token,
    get_student_by_public_id,
    session_counts,
    upsert_joined_student,
    whitelist_check_or_raise,
)
from ..views import templates


router = APIRouter()


@router.get("/join/{join_token}", response_class=HTMLResponse)
def join_page(request: Request, join_token: str):
    sess = get_session_by_join_token(join_token)
    session_id = str(sess["id"])
    counts = session_counts(session_id)
    return templates.TemplateResponse(
        "join.html",
        {"request": request, "sess": sess, "counts": counts, "join_token": join_token},
    )


@router.post("/join/{join_token}")
def join_submit(join_token: str, student_id: str = Form(...), name: str = Form(...)):
    sess = get_session_by_join_token(join_token)
    session_id = str(sess["id"])
    student_id = student_id.strip()
    name = name.strip()
    if not student_id or not name:
        raise HTTPException(400, "student_id and name required")

    whitelist_check_or_raise(session_id, student_id, name)
    _ = upsert_joined_student(session_id, student_id, name)

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    return RedirectResponse(url=f"/s/{session_id}/{student_id}", status_code=303)


@router.get("/s/{session_id}/{student_id}", response_class=HTMLResponse)
def student_page(request: Request, session_id: str, student_id: str):
    sess = get_session(session_id)
    stu = get_student_by_public_id(session_id, student_id)
    if not stu:
        return RedirectResponse(url=f"/join/{session_id}", status_code=303)
    return templates.TemplateResponse("student.html", {"request": request, "sess": sess, "stu": stu})


@router.post("/api/{session_id}/submit")
def api_submit(session_id: str, student_id: str = Form(...), contrib: str = Form(...)):
    sess = get_session(session_id)
    if int(sess["round_open"]) != 1 or int(sess["action_open"]) == 1:
        raise HTTPException(400, "Contribution stage is not open")

    parsed_contrib = ensure_int(contrib, 0, int(sess["endowment"]), "contrib")
    round_no = int(sess["current_round"])

    def _write_submit(conn: sqlite3.Connection) -> str:
        stu = conn.execute(
            "SELECT id FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
        if not stu:
            raise HTTPException(404, "student not found")

        sid = str(stu["id"])
        conn.execute(
            """
            INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(session_id, round_no, student_id)
            DO UPDATE SET contrib=excluded.contrib, created_at=excluded.created_at
        """,
            (session_id, round_no, sid, parsed_contrib, now_iso()),
        )
        return sid

    _run_write_with_retry(_write_submit)
    return {"ok": True, "round": round_no, "contrib": parsed_contrib}


@router.post("/api/{session_id}/submit_actions")
async def api_submit_actions(session_id: str, request: Request):
    sess = get_session(session_id)
    if int(sess["action_open"]) != 1:
        raise HTTPException(400, "Action stage is not open")

    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)
    if phase not in ("reward", "punishment"):
        raise HTTPException(400, "Current round has no reward/punishment stage")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")

    student_id = str(payload.get("student_id", "")).strip()
    allocations = payload.get("allocations")
    if not student_id:
        raise HTTPException(400, "student_id required")
    if not isinstance(allocations, dict):
        raise HTTPException(400, "allocations must be an object")

    def _write_actions(conn: sqlite3.Connection) -> int:
        stu = conn.execute(
            "SELECT id, group_no FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
        if not stu:
            raise HTTPException(404, "student not found")
        if stu["group_no"] is None:
            raise HTTPException(400, "group not assigned")

        actor_student_id = str(stu["id"])
        targets = conn.execute(
            """
            SELECT id, anonymous_id
            FROM students
            WHERE session_id=? AND group_no=? AND id<>?
            ORDER BY group_pos ASC, joined_at ASC
        """,
            (session_id, int(stu["group_no"]), actor_student_id),
        ).fetchall()

        target_by_anon = {target["anonymous_id"]: target["id"] for target in targets}

        rows_to_insert = []
        for anon_id, target_id in target_by_anon.items():
            raw_points = allocations.get(anon_id, 0)
            points = ensure_int(str(raw_points), 0, MAX_ACTION_POINTS, f"points[{anon_id}]")
            if points > 0:
                rows_to_insert.append((session_id, round_no, actor_student_id, target_id, points, now_iso()))

        conn.execute(
            "DELETE FROM actions WHERE session_id=? AND round_no=? AND actor_student_id=?",
            (session_id, round_no, actor_student_id),
        )

        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
                VALUES(?,?,?,?,?,?)
            """,
                rows_to_insert,
            )

        return len(rows_to_insert)

    targets_submitted = _run_write_with_retry(_write_actions)

    return {
        "ok": True,
        "round": round_no,
        "phase": phase,
        "targets_submitted": targets_submitted,
    }


@router.get("/api/{session_id}/status")
def api_status(session_id: str, student_id: str):
    return JSONResponse(build_student_status_payload(session_id, student_id))
