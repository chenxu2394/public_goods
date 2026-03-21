from __future__ import annotations

import math
import random
import sqlite3
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from .config import (
    ACTION_COST,
    DEFAULT_GROUP_SIZE,
    MAX_ACTION_POINTS,
    MAX_GROUP_SIZE,
    MIN_GROUP_SIZE,
    PHASE_LABELS,
    PHASE_ROUNDS,
    PHASES,
    PUNISH_EFFECT,
    REWARD_EFFECT,
)
from .db import db, now_iso


def phase_for_round(round_no: int) -> Tuple[str, int]:
    if round_no <= PHASE_ROUNDS:
        return "baseline", round_no
    if round_no <= PHASE_ROUNDS * 2:
        return "reward", round_no - PHASE_ROUNDS
    return "punishment", max(1, round_no - PHASE_ROUNDS * 2)


def phase_start_round(phase: str) -> int:
    if phase == "baseline":
        return 1
    if phase == "reward":
        return PHASE_ROUNDS + 1
    if phase == "punishment":
        return PHASE_ROUNDS * 2 + 1
    raise HTTPException(400, "Invalid phase")


def phase_round_count_for_session(total_rounds: int, phase: str) -> int:
    start = phase_start_round(phase)
    if total_rounds < start:
        return 0
    return min(PHASE_ROUNDS, total_rounds - start + 1)


def phase_round_bounds_for_session(total_rounds: int, phase: str) -> Optional[Tuple[int, int]]:
    phase_rounds = phase_round_count_for_session(total_rounds, phase)
    if phase_rounds <= 0:
        return None
    start = phase_start_round(phase)
    return start, start + phase_rounds - 1


def stage_of_session(sess: sqlite3.Row) -> str:
    if int(sess["action_open"]) == 1:
        return "action"
    if int(sess["round_open"]) == 1:
        return "contribution"
    return "closed"


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase.title())


def round_context(sess: sqlite3.Row) -> Dict[str, object]:
    cur = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur)
    stage = stage_of_session(sess)

    if stage == "contribution":
        if phase == "baseline":
            close_label = "Close contribution and compute round"
        else:
            close_label = f"Close contribution and open {phase_label(phase)} stage"
    elif stage == "action":
        close_label = f"Close {phase_label(phase)} stage and compute round"
    else:
        close_label = "Round is closed (open current round first)"

    return {
        "round": cur,
        "phase": phase,
        "phase_label": phase_label(phase),
        "phase_round": phase_round,
        "stage": stage,
        "close_label": close_label,
    }


def ensure_int(value: str, min_v: int, max_v: int, field: str) -> int:
    try:
        parsed = int(value)
    except Exception:
        raise HTTPException(400, f"{field} must be an integer")
    if parsed < min_v or parsed > max_v:
        raise HTTPException(400, f"{field} must be between {min_v} and {max_v}")
    return parsed


def next_round_after_compute(current_round: int, rounds: int) -> int:
    if current_round >= rounds:
        return rounds
    return current_round + 1


def count_computed_rounds(session_id: str) -> int:
    conn = db()
    computed = conn.execute(
        "SELECT COUNT(DISTINCT round_no) AS c FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    conn.close()
    return int(computed or 0)


def phase_computed_counts_conn(conn: sqlite3.Connection, session_id: str) -> Dict[str, int]:
    counts = {phase: 0 for phase in PHASES}
    rows = conn.execute(
        """
        SELECT round_no
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    for row in rows:
        phase, _ = phase_for_round(int(row["round_no"]))
        counts[phase] += 1
    return counts


def build_phase_status(total_rounds: int, computed_counts: Dict[str, int]) -> List[Dict[str, object]]:
    statuses = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0:
            continue
        statuses.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "total_rounds": phase_rounds,
                "computed_rounds": int(computed_counts.get(phase, 0)),
                "completed": int(computed_counts.get(phase, 0)) >= phase_rounds,
            }
        )
    return statuses


def build_student_phase_report_conn(
    conn: sqlite3.Connection,
    session_id: str,
    student_row_id: str,
    phase: str,
) -> Optional[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT round_no, phase_round, group_no, group_n, group_total, contrib,
               action_sent, action_received, action_cost, action_effect,
               income, phase_cumulative, computed_at
        FROM results
        WHERE session_id=? AND student_id=? AND phase=?
        ORDER BY round_no ASC
    """,
        (session_id, student_row_id, phase),
    ).fetchall()
    if not rows:
        return None

    group_no = int(rows[0]["group_no"])
    group_n = int(rows[0]["group_n"])
    group_round_rows = conn.execute(
        """
        SELECT round_no,
               MIN(phase_round) AS phase_round,
               MAX(group_total) AS group_total,
               SUM(income) AS group_income,
               AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=? AND phase=? AND group_no=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id, phase, group_no),
    ).fetchall()

    student_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "contrib": int(row["contrib"]),
            "action_sent": int(row["action_sent"]),
            "action_received": int(row["action_received"]),
            "action_cost": float(row["action_cost"]),
            "action_effect": float(row["action_effect"]),
            "income": float(row["income"]),
            "phase_cumulative": float(row["phase_cumulative"]),
            "computed_at": row["computed_at"],
        }
        for row in rows
    ]
    group_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "group_total": int(row["group_total"]),
            "group_income": float(row["group_income"]),
            "avg_contrib": float(row["avg_contrib"]),
        }
        for row in group_round_rows
    ]

    total_group_contrib = sum(row["group_total"] for row in group_rows)
    total_group_income = sum(row["group_income"] for row in group_rows)

    return {
        "phase": phase,
        "phase_label": phase_label(phase),
        "group_no": group_no,
        "group_n": group_n,
        "student_summary": {
            "total_contrib": sum(row["contrib"] for row in student_rows),
            "total_income": sum(row["income"] for row in student_rows),
            "action_sent": sum(row["action_sent"] for row in student_rows),
            "action_received": sum(row["action_received"] for row in student_rows),
            "final_phase_cumulative": student_rows[-1]["phase_cumulative"],
        },
        "student_rows": student_rows,
        "group_summary": {
            "total_contrib": total_group_contrib,
            "total_income": total_group_income,
            "avg_contrib": (total_group_contrib / (group_n * len(group_rows))) if group_rows and group_n > 0 else 0.0,
        },
        "group_rows": group_rows,
    }


def build_teacher_phase_reports_conn(
    conn: sqlite3.Connection,
    session_id: str,
    total_rounds: int,
    computed_counts: Dict[str, int],
) -> List[Dict[str, object]]:
    reports = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0 or int(computed_counts.get(phase, 0)) < phase_rounds:
            continue

        group_rows = conn.execute(
            """
            SELECT group_no,
                   MAX(group_n) AS group_n,
                   SUM(group_total) AS total_contrib,
                   SUM(group_income) AS total_income,
                   AVG(avg_contrib) AS avg_contrib
            FROM (
                SELECT group_no,
                       round_no,
                       MAX(group_n) AS group_n,
                       MAX(group_total) AS group_total,
                       SUM(income) AS group_income,
                       AVG(contrib) AS avg_contrib
                FROM results
                WHERE session_id=? AND phase=?
                GROUP BY group_no, round_no
            ) per_round
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, phase),
        ).fetchall()

        student_rows = conn.execute(
            """
            SELECT s.anonymous_id, s.student_id, s.name, r.group_no,
                   SUM(r.contrib) AS total_contrib,
                   SUM(r.income) AS total_income,
                   SUM(r.action_sent) AS action_sent,
                   SUM(r.action_received) AS action_received,
                   MAX(r.phase_cumulative) AS final_phase_cumulative
            FROM results r
            JOIN students s ON s.id=r.student_id
            WHERE r.session_id=? AND r.phase=?
            GROUP BY r.student_id, s.anonymous_id, s.student_id, s.name, r.group_no, s.group_pos, s.joined_at
            ORDER BY r.group_no ASC, s.group_pos ASC, s.joined_at ASC
        """,
            (session_id, phase),
        ).fetchall()

        groups = [
            {
                "group_no": int(row["group_no"]),
                "group_n": int(row["group_n"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "avg_contrib": float(row["avg_contrib"]),
            }
            for row in group_rows
        ]
        students = [
            {
                "anonymous_id": row["anonymous_id"],
                "student_id": row["student_id"],
                "name": row["name"],
                "group_no": int(row["group_no"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "action_sent": int(row["action_sent"]),
                "action_received": int(row["action_received"]),
                "final_phase_cumulative": float(row["final_phase_cumulative"]),
            }
            for row in student_rows
        ]

        reports.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "groups": groups,
                "students": students,
                "totals": {
                    "total_contrib": sum(row["total_contrib"] for row in groups),
                    "total_income": sum(row["total_income"] for row in groups),
                },
            }
        )
    return reports


def current_round_progress_conn(
    conn: sqlite3.Connection,
    session_id: str,
    round_no: int,
) -> Dict[str, object]:
    group_rows = conn.execute(
        """
        SELECT s.group_no,
               COUNT(DISTINCT s.id) AS student_total,
               COUNT(DISTINCT c.student_id) AS contrib_submitted,
               COUNT(DISTINCT a.actor_student_id) AS action_submitted
        FROM students s
        LEFT JOIN contributions c
          ON c.session_id=s.session_id
         AND c.round_no=?
         AND c.student_id=s.id
        LEFT JOIN actions a
          ON a.session_id=s.session_id
         AND a.round_no=?
         AND a.actor_student_id=s.id
        WHERE s.session_id=? AND s.group_no IS NOT NULL
        GROUP BY s.group_no
        ORDER BY s.group_no ASC
    """,
        (round_no, round_no, session_id),
    ).fetchall()

    groups = [
        {
            "group_no": int(row["group_no"]),
            "student_total": int(row["student_total"]),
            "contrib_submitted": int(row["contrib_submitted"] or 0),
            "action_submitted": int(row["action_submitted"] or 0),
        }
        for row in group_rows
    ]
    return {
        "student_total": sum(row["student_total"] for row in groups),
        "contrib_submitted": sum(row["contrib_submitted"] for row in groups),
        "action_submitted": sum(row["action_submitted"] for row in groups),
        "groups": groups,
    }


def current_round_contrib_rows_conn(
    conn: sqlite3.Connection,
    session_id: str,
    round_no: int,
) -> List[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT s.group_no, s.anonymous_id, s.student_id, s.name, COALESCE(c.contrib, 0) AS contrib
        FROM students s
        LEFT JOIN contributions c
          ON c.session_id=s.session_id
         AND c.round_no=?
         AND c.student_id=s.id
        WHERE s.session_id=? AND s.group_no IS NOT NULL
        ORDER BY s.group_no ASC, s.group_pos ASC, s.joined_at ASC
    """,
        (round_no, session_id),
    ).fetchall()
    return [
        {
            "group_no": int(row["group_no"]),
            "anonymous_id": row["anonymous_id"],
            "student_id": row["student_id"],
            "name": row["name"],
            "contrib": int(row["contrib"]),
        }
        for row in rows
    ]


def _choose_group_sizes(total: int, preferred_size: int) -> List[int]:
    if total <= 0:
        return []
    if total < MIN_GROUP_SIZE:
        return [total]
    if total <= MAX_GROUP_SIZE:
        return [total]

    preferred = max(MIN_GROUP_SIZE, min(MAX_GROUP_SIZE, int(preferred_size or DEFAULT_GROUP_SIZE)))
    min_groups = math.ceil(total / MAX_GROUP_SIZE)
    max_groups = max(min_groups, total // MIN_GROUP_SIZE)

    best_sizes: Optional[List[int]] = None
    best_score: Optional[Tuple[float, int]] = None
    target_groups = max(1, round(total / preferred))

    for group_count in range(min_groups, max_groups + 1):
        base = total // group_count
        rem = total % group_count
        sizes = [base + 1 if i < rem else base for i in range(group_count)]
        if min(sizes) < MIN_GROUP_SIZE or max(sizes) > MAX_GROUP_SIZE:
            continue

        score = (abs((total / group_count) - preferred), abs(group_count - target_groups))
        if best_score is None or score < best_score:
            best_score = score
            best_sizes = sizes

    if best_sizes is not None:
        return best_sizes

    group_count = max(1, round(total / preferred))
    base = total // group_count
    rem = total % group_count
    return [base + 1 if i < rem else base for i in range(group_count)]


def lock_groups(session_id: str, group_size: int):
    conn = db()
    students = conn.execute(
        """
        SELECT id FROM students
        WHERE session_id=?
        ORDER BY joined_at ASC
    """,
        (session_id,),
    ).fetchall()
    ids = [row["id"] for row in students]

    random.shuffle(ids)
    sizes = _choose_group_sizes(len(ids), group_size)

    updates = []
    idx = 0
    for group_no, size in enumerate(sizes, start=1):
        for pos in range(1, size + 1):
            if idx >= len(ids):
                break
            updates.append((group_no, pos, ids[idx]))
            idx += 1

    if updates:
        conn.executemany("UPDATE students SET group_no=?, group_pos=? WHERE id=?", updates)
    conn.execute("UPDATE sessions SET locked=1 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def assign_late_joiner(session_id: str) -> None:
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        conn.close()
        return

    late = conn.execute(
        """
        SELECT id FROM students
        WHERE session_id=? AND group_no IS NULL
        ORDER BY joined_at ASC
    """,
        (session_id,),
    ).fetchall()
    if not late:
        conn.close()
        return

    max_group_row = conn.execute(
        """
        SELECT MAX(group_no) AS g FROM students
        WHERE session_id=? AND group_no IS NOT NULL
    """,
        (session_id,),
    ).fetchone()
    group_no = int(max_group_row["g"] or 1)

    count_row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM students
        WHERE session_id=? AND group_no=?
    """,
        (session_id, group_no),
    ).fetchone()
    current_count = int(count_row["c"])

    updates = []
    for row in late:
        if current_count >= MAX_GROUP_SIZE:
            group_no += 1
            current_count = 0
        current_count += 1
        updates.append((group_no, current_count, row["id"]))

    if updates:
        conn.executemany("UPDATE students SET group_no=?, group_pos=? WHERE id=?", updates)
    conn.commit()
    conn.close()


def open_round(session_id: str, round_no: int):
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=1, action_open=0 WHERE id=?",
        (round_no, session_id),
    )
    conn.commit()
    conn.close()


def close_round(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=0 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def open_action_stage(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=1 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def advance_round(session_id: str, current_round: int, rounds: int) -> None:
    nxt = next_round_after_compute(current_round, rounds)
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=0, action_open=0 WHERE id=?",
        (nxt, session_id),
    )
    conn.commit()
    conn.close()


def compute_results(session_id: str, round_no: int):
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        conn.close()
        raise HTTPException(404, "Session not found")

    multiplier = float(sess["multiplier"])
    endowment = int(sess["endowment"])
    phase, phase_round = phase_for_round(round_no)

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
        SELECT student_id, cumulative FROM results
        WHERE session_id=? AND round_no=?
    """,
        (session_id, round_no - 1),
    ).fetchall()
    prev_cum = {row["student_id"]: float(row["cumulative"]) for row in prev}
    prev_phase_cum: Dict[str, float] = {}
    if phase_round > 1:
        prev_phase_rows = conn.execute(
            """
            SELECT student_id, phase_cumulative
            FROM results
            WHERE session_id=? AND round_no=?
        """,
            (session_id, round_no - 1),
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
