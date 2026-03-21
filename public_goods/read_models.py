from __future__ import annotations

import base64
import csv
import io
import sqlite3
from typing import Dict, List, Tuple, TypedDict

import segno
from fastapi import HTTPException, Request

from .auth import _auth_configuration_error, _is_auth_configured
from .config import (
    DEMO_DEFAULT_STUDENT_COUNT,
    DEMO_MAX_STUDENT_COUNT,
    PUBLIC_BASE_URL,
    USER_ROLE_ADMIN,
)
from .db import db
from .experiment import (
    build_phase_status,
    build_student_phase_report_conn,
    build_teacher_phase_reports_conn,
    count_computed_rounds,
    current_round_contrib_rows_conn,
    current_round_progress_conn,
    phase_computed_counts_conn,
    phase_for_round,
    phase_round_count_for_session,
    round_context,
    stage_of_session,
)
from .sessions import get_session, list_sessions, list_students, list_teachers, session_counts


class ShareLinkContext(TypedDict):
    join_url: str
    join_qr_data_uri: str


class LoginPageContext(TypedDict, total=False):
    request: Request
    configured: bool
    configuration_error: str
    pw_changed: bool
    error: str


class AdminHomeContext(TypedDict, total=False):
    request: Request
    user: sqlite3.Row
    is_admin: bool
    must_change_password: bool
    pw_change_required: bool
    sessions: List[sqlite3.Row]
    teachers: List[sqlite3.Row]
    pw_error: str
    teacher_error: str
    teacher_success: str
    teacher_temp_password: str
    teacher_temp_password_username: str


class SessionPanelContext(TypedDict, total=False):
    request: Request
    user: sqlite3.Row
    is_admin: bool
    sess: sqlite3.Row
    students: List[sqlite3.Row]
    counts: Dict[str, int]
    join_url: str
    join_qr_data_uri: str
    share_url: str
    export_url: str
    template_url: str
    display_url: str
    round_ctx: Dict[str, object]
    computed_rounds: int
    phase_statuses: List[Dict[str, object]]
    round_progress: Dict[str, object]
    current_round_contrib_rows: List[Dict[str, object]]
    phase_reports: List[Dict[str, object]]
    demo_default_student_count: int
    transfer_teachers: List[sqlite3.Row]
    title_error: str
    join_link_success: str
    transfer_error: str
    transfer_success: str
    demo_error: str
    demo_success: str


class StudentStatusPayload(TypedDict):
    session: Dict[str, object]
    student: Dict[str, object]
    phase_statuses: List[Dict[str, object]]
    current_phase: Dict[str, object]
    current_round: Dict[str, object]
    group_view: List[Dict[str, object]]
    action_targets: List[Dict[str, object]]
    completed_phases: List[Dict[str, object]]


class DisplayStatusPayload(TypedDict):
    session: Dict[str, object]
    computed_rounds: int
    latest_computed_round: int | None
    latest_groups: List[Dict[str, object]]
    avg_series: List[Dict[str, object]]
    overall_avg_contrib: float | None


def qr_svg_data_uri(content: str) -> str:
    qr = segno.make_qr(content, error="m")
    output = io.BytesIO()
    qr.save(output, kind="svg", scale=6, border=2, dark="#111111", light="#ffffff")
    svg = output.getvalue()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def build_share_link_context(sess: sqlite3.Row) -> ShareLinkContext:
    join_url = f"{PUBLIC_BASE_URL}/join/{sess['join_token']}"
    return {
        "join_url": join_url,
        "join_qr_data_uri": qr_svg_data_uri(join_url),
    }


def build_login_page_context(request: Request, **extra: object) -> LoginPageContext:
    context: LoginPageContext = {
        "request": request,
        "configured": _is_auth_configured(),
        "configuration_error": _auth_configuration_error(),
        "pw_changed": request.query_params.get("pw_changed") == "1",
    }
    context.update(extra)
    return context


def build_admin_home_context(request: Request, user: sqlite3.Row, **extra: object) -> AdminHomeContext:
    is_admin = user["role"] == USER_ROLE_ADMIN
    must_change_password = int(user["must_change_password"]) == 1
    context: AdminHomeContext = {
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
    return context


def build_share_link_page_context(request: Request, sess: sqlite3.Row) -> Dict[str, object]:
    context: Dict[str, object] = {
        "request": request,
        "sess": sess,
        "share_api_url": f"{PUBLIC_BASE_URL}/api/admin/{sess['id']}/share_link",
    }
    context.update(build_share_link_context(sess))
    return context


def build_share_link_api_payload(sess: sqlite3.Row) -> Dict[str, object]:
    payload = build_share_link_context(sess)
    return {
        "session": {
            "id": sess["id"],
            "title": sess["title"],
        },
        "join_url": payload["join_url"],
        "join_qr_data_uri": payload["join_qr_data_uri"],
    }


def build_student_status_payload(session_id: str, student_id: str) -> StudentStatusPayload:
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

    return {
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


def build_display_status_payload(session_id: str) -> DisplayStatusPayload:
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

    return {
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


def build_export_csv(session_id: str, rounds: int) -> Tuple[str, bytes]:
    conn = db()
    students = conn.execute(
        """
        SELECT id, anonymous_id, student_id, name, group_no
        FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()

    contrib_rows = conn.execute(
        "SELECT round_no, student_id, contrib FROM contributions WHERE session_id=?",
        (session_id,),
    ).fetchall()
    contrib_map = {(int(row["round_no"]), row["student_id"]): int(row["contrib"]) for row in contrib_rows}

    result_rows = conn.execute(
        """
        SELECT round_no, student_id, phase, phase_round, income, cumulative, phase_cumulative, action_sent, action_received
        FROM results
        WHERE session_id=?
    """,
        (session_id,),
    ).fetchall()
    result_map = {(int(row["round_no"]), row["student_id"]): row for row in result_rows}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "experiment_id",
            "anonymous_id",
            "student_id",
            "name",
            "phase",
            "phase_round",
            "round_no",
            "contribution",
            "income",
            "cumulative",
            "phase_cumulative",
            "action_sent",
            "action_received",
        ]
    )

    for student in students:
        for round_no in range(1, rounds + 1):
            default_phase, default_phase_round = phase_for_round(round_no)
            contrib = contrib_map.get((round_no, student["id"]), "")
            result_row = result_map.get((round_no, student["id"]))
            if result_row:
                phase = result_row["phase"]
                phase_round = result_row["phase_round"]
                income = result_row["income"]
                cumulative = result_row["cumulative"]
                phase_cumulative = result_row["phase_cumulative"]
                action_sent = result_row["action_sent"]
                action_received = result_row["action_received"]
            else:
                phase = default_phase
                phase_round = default_phase_round
                income = ""
                cumulative = ""
                phase_cumulative = ""
                action_sent = ""
                action_received = ""

            writer.writerow(
                [
                    session_id,
                    student["anonymous_id"],
                    student["student_id"],
                    student["name"],
                    phase,
                    phase_round,
                    round_no,
                    contrib,
                    income,
                    cumulative,
                    phase_cumulative,
                    action_sent,
                    action_received,
                ]
            )

    data = output.getvalue().encode("utf-8-sig")
    filename = f"public_goods_{session_id}.csv"
    return filename, data
