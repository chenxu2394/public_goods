from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import MAX_ACTION_POINTS
from ..db import _run_write_with_retry, db, now_iso
from ..experiment import (
    build_phase_status,
    build_student_phase_report_conn,
    ensure_int,
    phase_computed_counts_conn,
    phase_for_round,
    phase_round_count_for_session,
    stage_of_session,
)
from ..sessions import (
    get_session,
    get_session_by_join_token,
    get_student_by_public_id,
    session_counts,
    upsert_joined_student,
    whitelist_check_or_raise,
)
from ..views import templates
from ..experiment import assign_late_joiner


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
    sess = get_session(session_id)
    conn = db()
    stu = conn.execute(
        "SELECT * FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if not stu:
        conn.close()
        raise HTTPException(404, "student not found")

    cur_r = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_r)
    stage = stage_of_session(sess)
    total_rounds = int(sess["rounds"])
    computed_counts = phase_computed_counts_conn(conn, session_id)
    phase_statuses = build_phase_status(total_rounds, computed_counts)
    current_phase_report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), phase)

    cur_c = conn.execute(
        """
        SELECT contrib FROM contributions
        WHERE session_id=? AND round_no=? AND student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchone()

    submitted_actions_rows = conn.execute(
        """
        SELECT target_student_id, points
        FROM actions
        WHERE session_id=? AND round_no=? AND actor_student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchall()
    submitted_actions = {row["target_student_id"]: int(row["points"]) for row in submitted_actions_rows}

    group_view_rows = []
    action_targets = []
    group_view_visible = stage == "action" and phase in ("reward", "punishment") and stu["group_no"] is not None
    if group_view_visible:
        group_view_rows = conn.execute(
            """
            SELECT s.id, s.anonymous_id, COALESCE(c.contrib, 0) AS contrib
            FROM students s
            LEFT JOIN contributions c
                ON c.session_id=s.session_id
               AND c.round_no=?
               AND c.student_id=s.id
            WHERE s.session_id=? AND s.group_no=?
            ORDER BY s.group_pos ASC, s.joined_at ASC
        """,
            (cur_r, session_id, int(stu["group_no"])),
        ).fetchall()

        for row in group_view_rows:
            if row["id"] == stu["id"]:
                continue
            action_targets.append(
                {
                    "anonymous_id": row["anonymous_id"],
                    "points": submitted_actions.get(row["id"], 0),
                }
            )

    completed_phases = []
    for phase_status in phase_statuses:
        if not phase_status["completed"]:
            continue
        report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), str(phase_status["phase"]))
        if report is None:
            continue
        completed_phases.append(report)

    conn.close()

    current_phase_summary = {
        "phase": phase,
        "phase_label": phase.title(),
        "phase_round": phase_round,
        "computed_rounds": int(computed_counts.get(phase, 0)),
        "total_rounds": phase_round_count_for_session(total_rounds, phase),
        "student_phase_cumulative": 0.0,
        "student_total_contrib": 0,
        "group_no": int(stu["group_no"]) if stu["group_no"] is not None else None,
        "group_phase_cumulative": 0.0,
        "group_total_contrib": 0,
    }
    if current_phase_report is not None:
        current_phase_summary.update(
            {
                "phase_label": current_phase_report["phase_label"],
                "student_phase_cumulative": float(current_phase_report["student_summary"]["final_phase_cumulative"]),
                "student_total_contrib": int(current_phase_report["student_summary"]["total_contrib"]),
                "group_no": int(current_phase_report["group_no"]),
                "group_phase_cumulative": float(current_phase_report["group_summary"]["total_income"]),
                "group_total_contrib": int(current_phase_report["group_summary"]["total_contrib"]),
            }
        )

    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
                "group_size": int(sess["group_size"]),
                "multiplier": float(sess["multiplier"]),
                "endowment": int(sess["endowment"]),
                "rounds": int(sess["rounds"]),
                "locked": bool(int(sess["locked"])),
                "current_round": cur_r,
                "round_open": bool(int(sess["round_open"])),
                "action_open": bool(int(sess["action_open"])),
                "phase": phase,
                "phase_label": current_phase_summary["phase_label"],
                "phase_round": phase_round,
                "stage": stage,
            },
            "student": {
                "student_id": stu["student_id"],
                "name": stu["name"],
                "anonymous_id": stu["anonymous_id"],
                "group_no": stu["group_no"],
            },
            "phase_statuses": phase_statuses,
            "current_phase": current_phase_summary,
            "current_round": {
                "round": cur_r,
                "submitted_contrib": int(cur_c["contrib"]) if cur_c else None,
                "group_view_visible": group_view_visible,
            },
            "group_view": [
                {"anonymous_id": row["anonymous_id"], "contrib": int(row["contrib"])}
                for row in group_view_rows
                if row["id"] != stu["id"]
            ],
            "action_targets": action_targets,
            "completed_phases": completed_phases,
        }
    )
