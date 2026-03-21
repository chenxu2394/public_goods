from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import _management_gate
from ..db import db
from ..experiment import phase_for_round, stage_of_session
from ..sessions import get_session, get_session_for_user
from ..views import _share_link_context, templates


router = APIRouter()


@router.get("/display/{session_id}", response_class=HTMLResponse)
def display_page(request: Request, session_id: str):
    sess = get_session(session_id)
    return templates.TemplateResponse("display.html", {"request": request, "sess": sess})


@router.get("/api/{session_id}/display_status")
def api_display_status(session_id: str):
    sess = get_session(session_id)
    cur_round = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_round)
    stage = stage_of_session(sess)

    conn = db()
    latest_row = conn.execute(
        "SELECT MAX(round_no) AS r FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()
    latest_round = latest_row["r"]

    latest_groups = []
    if latest_round is not None:
        rows = conn.execute(
            """
            SELECT group_no, COUNT(*) AS group_n, SUM(contrib) AS group_total, AVG(contrib) AS avg_contrib
            FROM results
            WHERE session_id=? AND round_no=?
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, int(latest_round)),
        ).fetchall()
        latest_groups = [
            {
                "group_no": int(row["group_no"]),
                "group_n": int(row["group_n"]),
                "group_total": int(row["group_total"]),
                "avg_contrib": float(row["avg_contrib"]),
            }
            for row in rows
        ]

    series_rows = conn.execute(
        """
        SELECT round_no, AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    series = [{"round": int(row["round_no"]), "avg_contrib": float(row["avg_contrib"])} for row in series_rows]

    overall = conn.execute(
        "SELECT AVG(contrib) AS v FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["v"]
    conn.close()

    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
                "rounds": int(sess["rounds"]),
                "current_round": cur_round,
                "phase": phase,
                "phase_label": phase.title(),
                "phase_round": phase_round,
                "stage": stage,
            },
            "computed_rounds": len(series),
            "latest_computed_round": int(latest_round) if latest_round is not None else None,
            "latest_groups": latest_groups,
            "avg_series": series,
            "overall_avg_contrib": float(overall) if overall is not None else None,
        }
    )


@router.get("/api/admin/{session_id}/share_link")
def api_share_link_status(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    payload = _share_link_context(sess)
    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
            },
            "join_url": payload["join_url"],
            "join_qr_data_uri": payload["join_qr_data_uri"],
        }
    )
