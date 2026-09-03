from __future__ import annotations

from typing import Dict

from fastapi import HTTPException

from ..config import ACTION_COST, PUNISH_EFFECT, REWARD_EFFECT
from ..db import db, now_iso
from .grouping import assign_late_joiner
from .phases import phase_context_conn


def compute_results(session_id: str, round_no: int):
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        conn.close()
        raise HTTPException(404, "Session not found")

    multiplier = float(sess["multiplier"])
    endowment = int(sess["endowment"])
    if int(sess["current_round"]) != round_no:
        conn.close()
        raise HTTPException(400, "Only the current round can be computed.")
    phase, phase_round = phase_context_conn(conn, sess)
    if phase is None or phase_round is None:
        conn.close()
        raise HTTPException(400, "Choose a round type before computing this round.")

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    students = conn.execute(
        """
        SELECT id, group_no
        FROM students
        WHERE session_id=? AND group_no IS NOT NULL
        ORDER BY group_no ASC, group_pos ASC
    """,
        (session_id,),
    ).fetchall()

    contrib_rows = conn.execute(
        """
        SELECT student_id, contrib FROM contributions
        WHERE session_id=? AND round_no=?
    """,
        (session_id, round_no),
    ).fetchall()
    contrib = {row["student_id"]: int(row["contrib"]) for row in contrib_rows}

    group_totals: Dict[int, int] = {}
    group_ns: Dict[int, int] = {}
    student_group: Dict[str, int] = {}
    for student in students:
        group_no = int(student["group_no"])
        student_id = student["id"]
        student_group[student_id] = group_no
        group_ns[group_no] = group_ns.get(group_no, 0) + 1
        group_totals[group_no] = group_totals.get(group_no, 0) + contrib.get(student_id, 0)

    prev = conn.execute(
        """
        SELECT r.student_id, r.cumulative
        FROM results r
        JOIN (
            SELECT student_id, MAX(round_no) AS round_no
            FROM results
            WHERE session_id=? AND round_no<?
            GROUP BY student_id
        ) latest ON latest.student_id=r.student_id AND latest.round_no=r.round_no
        WHERE r.session_id=?
    """,
        (session_id, round_no, session_id),
    ).fetchall()
    prev_cum = {row["student_id"]: float(row["cumulative"]) for row in prev}
    prev_phase_cum: Dict[str, float] = {}
    if phase_round > 1:
        prev_phase_rows = conn.execute(
            """
            SELECT r.student_id, r.phase_cumulative
            FROM results r
            JOIN (
                SELECT student_id, MAX(round_no) AS round_no
                FROM results
                WHERE session_id=? AND phase=? AND round_no<?
                GROUP BY student_id
            ) latest ON latest.student_id=r.student_id AND latest.round_no=r.round_no
            WHERE r.session_id=? AND r.phase=?
        """,
            (session_id, phase, round_no, session_id, phase),
        ).fetchall()
        prev_phase_cum = {row["student_id"]: float(row["phase_cumulative"]) for row in prev_phase_rows}

    action_sent: Dict[str, int] = {}
    action_received: Dict[str, int] = {}
    if phase in ("reward", "punishment"):
        action_rows = conn.execute(
            """
            SELECT actor_student_id, target_student_id, points
            FROM actions
            WHERE session_id=? AND round_no=?
        """,
            (session_id, round_no),
        ).fetchall()
        for row in action_rows:
            actor = row["actor_student_id"]
            target = row["target_student_id"]
            points = int(row["points"])
            if points <= 0 or actor == target:
                continue
            if actor not in student_group or target not in student_group:
                continue
            if student_group[actor] != student_group[target]:
                continue
            action_sent[actor] = action_sent.get(actor, 0) + points
            action_received[target] = action_received.get(target, 0) + points

    computed_at = now_iso()
    out_rows = []
    for student in students:
        student_id = student["id"]
        group_no = int(student["group_no"])
        group_total = int(group_totals[group_no])
        group_n = int(group_ns[group_no])
        public_return = multiplier * group_total / group_n if group_n > 0 else 0.0
        student_contrib = contrib.get(student_id, 0)

        base_income = endowment - student_contrib + public_return
        sent = action_sent.get(student_id, 0)
        received = action_received.get(student_id, 0)

        if phase == "reward":
            action_cost = ACTION_COST * sent
            action_effect = REWARD_EFFECT * received
        elif phase == "punishment":
            action_cost = ACTION_COST * sent
            action_effect = -PUNISH_EFFECT * received
        else:
            action_cost = 0.0
            action_effect = 0.0

        income = base_income - action_cost + action_effect
        cumulative = prev_cum.get(student_id, 0.0) + income
        phase_cumulative = prev_phase_cum.get(student_id, 0.0) + income

        out_rows.append(
            (
                session_id,
                round_no,
                student_id,
                group_no,
                group_n,
                group_total,
                public_return,
                student_contrib,
                phase,
                phase_round,
                sent,
                received,
                action_cost,
                action_effect,
                income,
                cumulative,
                phase_cumulative,
                computed_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, phase, phase_round, action_sent, action_received,
            action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id, round_no, student_id)
        DO UPDATE SET
            group_no=excluded.group_no,
            group_n=excluded.group_n,
            group_total=excluded.group_total,
            public_return=excluded.public_return,
            contrib=excluded.contrib,
            phase=excluded.phase,
            phase_round=excluded.phase_round,
            action_sent=excluded.action_sent,
            action_received=excluded.action_received,
            action_cost=excluded.action_cost,
            action_effect=excluded.action_effect,
            income=excluded.income,
            cumulative=excluded.cumulative,
            phase_cumulative=excluded.phase_cumulative,
            computed_at=excluded.computed_at
    """,
        out_rows,
    )

    conn.commit()
    conn.close()
