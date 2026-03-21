from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sqlite3
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from .config import DEFAULT_GROUP_SIZE, DEMO_PROFILES, MAX_ACTION_POINTS
from .db import _generate_anonymous_id, db, now_iso
from .experiment import (
    advance_round,
    close_round,
    compute_results,
    lock_groups,
    open_action_stage,
    open_round,
    phase_computed_counts_conn,
    phase_for_round,
    phase_label,
    phase_round_bounds_for_session,
    phase_round_count_for_session,
    stage_of_session,
)
from .sessions import get_session


def _stable_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _demo_profile_for_student(student_public_id: str) -> str:
    digits = "".join(ch for ch in student_public_id if ch.isdigit())
    index = int(digits) if digits else _stable_int(student_public_id)
    return DEMO_PROFILES[index % len(DEMO_PROFILES)]


def _demo_student_rows(student_count: int) -> List[Tuple[str, str]]:
    return [(f"DEMO{i:03d}", f"Demo Student {i:02d}") for i in range(1, student_count + 1)]


def create_demo_class(session_id: str, student_count: int) -> int:
    conn = db()
    group_size = DEFAULT_GROUP_SIZE
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        group_size = int(sess["group_size"])
        if int(sess["round_open"]) == 1 or int(sess["action_open"]) == 1:
            raise HTTPException(400, "Close the current round before creating a demo class.")

        students_count = conn.execute(
            "SELECT COUNT(*) AS c FROM students WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        contrib_count = conn.execute(
            "SELECT COUNT(*) AS c FROM contributions WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        action_count = conn.execute(
            "SELECT COUNT(*) AS c FROM actions WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        result_count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        if any(int(count or 0) > 0 for count in (students_count, contrib_count, action_count, result_count)):
            raise HTTPException(
                400,
                "Demo class creation requires a fresh session with no students or round data yet.",
            )

        conn.execute("DELETE FROM whitelist WHERE session_id=?", (session_id,))
        conn.execute(
            """
            UPDATE sessions
            SET demo_mode=1, locked=0, current_round=1, round_open=0, action_open=0
            WHERE id=?
        """,
            (session_id,),
        )

        base_time = dt.datetime.now().replace(microsecond=0)
        demo_students = _demo_student_rows(student_count)
        whitelist_rows = []
        student_rows = []
        used_anonymous_ids: set[str] = set()
        for idx, (student_public_id, name) in enumerate(demo_students):
            ts = (base_time + dt.timedelta(seconds=idx)).isoformat(timespec="seconds")
            whitelist_rows.append((session_id, student_public_id, name, ts))
            anonymous_id = _generate_anonymous_id(used_anonymous_ids)
            used_anonymous_ids.add(anonymous_id)
            student_rows.append(
                (
                    secrets.token_urlsafe(8),
                    session_id,
                    student_public_id,
                    name,
                    anonymous_id,
                    ts,
                    None,
                    None,
                )
            )

        conn.executemany(
            """
            INSERT INTO whitelist(session_id, student_id, name, added_at)
            VALUES(?,?,?,?)
        """,
            whitelist_rows,
        )
        conn.executemany(
            """
            INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            student_rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    lock_groups(session_id, group_size)
    return student_count


def _demo_previous_result_conn(
    conn: sqlite3.Connection,
    session_id: str,
    student_row_id: str,
    round_no: int,
) -> Optional[sqlite3.Row]:
    if round_no <= 1:
        return None
    return conn.execute(
        """
        SELECT group_total, group_n, action_received
        FROM results
        WHERE session_id=? AND student_id=? AND round_no=?
    """,
        (session_id, student_row_id, round_no - 1),
    ).fetchone()


def _demo_contribution_for_student(
    student_public_id: str,
    endowment: int,
    phase: str,
    round_no: int,
    previous_result: Optional[sqlite3.Row],
) -> int:
    profile = _demo_profile_for_student(student_public_id)
    base_by_profile = {
        "cooperator": round(endowment * 0.8),
        "conditional": round(endowment * 0.5),
        "reciprocator": round(endowment * 0.6),
        "free_rider": round(endowment * 0.2),
    }
    target = int(base_by_profile[profile])

    if phase == "reward":
        if profile in {"cooperator", "reciprocator"}:
            target += 1
    elif phase == "punishment":
        if profile == "free_rider":
            target += 1

    if previous_result is not None:
        group_avg = float(previous_result["group_total"]) / max(1, int(previous_result["group_n"]))
        received = int(previous_result["action_received"] or 0)
        if profile == "conditional":
            target = round(group_avg)
        elif profile == "reciprocator":
            target = round((target + group_avg) / 2)
            if received > 0:
                target += 1 if phase == "reward" else 2
        elif profile == "free_rider":
            if phase == "punishment" and received > 0:
                target += min(3, received)
            elif phase == "reward" and received > 0:
                target += 1
        else:
            target = max(target, round(group_avg))

    noise = (_stable_int(f"{student_public_id}:{round_no}") % 3) - 1
    return max(0, min(endowment, int(target + noise)))


def simulate_demo_contributions(session_id: str, round_no: int) -> int:
    conn = db()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        if int(sess["demo_mode"]) != 1:
            raise HTTPException(400, "Demo automation is available only for demo sessions.")
        if int(sess["locked"]) != 1:
            raise HTTPException(400, "Demo session must be grouped before auto-submitting contributions.")
        if int(sess["round_open"]) != 1 or int(sess["action_open"]) == 1:
            raise HTTPException(400, "Contribution stage must be open before auto-submitting contributions.")

        phase, _ = phase_for_round(round_no)
        endowment = int(sess["endowment"])
        students = conn.execute(
            """
            SELECT id, student_id
            FROM students
            WHERE session_id=? AND group_no IS NOT NULL
            ORDER BY group_no ASC, group_pos ASC, joined_at ASC
        """,
            (session_id,),
        ).fetchall()
        existing = {
            row["student_id"]
            for row in conn.execute(
                "SELECT student_id FROM contributions WHERE session_id=? AND round_no=?",
                (session_id, round_no),
            ).fetchall()
        }

        rows_to_insert = []
        for student in students:
            if student["id"] in existing:
                continue
            previous_result = _demo_previous_result_conn(conn, session_id, str(student["id"]), round_no)
            contrib = _demo_contribution_for_student(
                str(student["student_id"]),
                endowment,
                phase,
                round_no,
                previous_result,
            )
            rows_to_insert.append((session_id, round_no, student["id"], contrib, now_iso()))

        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(session_id, round_no, student_id)
                DO UPDATE SET contrib=excluded.contrib, created_at=excluded.created_at
            """,
                rows_to_insert,
            )
            conn.commit()
        return len(rows_to_insert)
    finally:
        conn.close()


def _demo_action_points(
    phase: str,
    actor_profile: str,
    actor_contrib: int,
    target_contrib: int,
    group_avg: float,
    is_top_target: bool,
) -> int:
    if phase == "reward":
        if target_contrib < group_avg:
            return 0
        if actor_profile == "free_rider":
            return 1 if is_top_target and target_contrib >= actor_contrib + 2 else 0
        if actor_profile == "conditional":
            return 2 if target_contrib >= group_avg + 1 else 1
        if actor_profile == "reciprocator":
            return 3 if is_top_target else 1
        return 2 if target_contrib >= group_avg + 1 else 1

    gap = group_avg - target_contrib
    if gap <= 0:
        return 0
    if actor_profile == "free_rider":
        return 1 if gap >= 3 else 0
    if actor_profile == "conditional":
        return 2 if gap >= 2 else 1
    if actor_profile == "reciprocator":
        return 3 if gap >= 3 else 2
    if actor_contrib >= round(group_avg):
        return 3 if gap >= 2 else 2
    return 2 if gap >= 2 else 1


def simulate_demo_actions(session_id: str, round_no: int) -> int:
    conn = db()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        if int(sess["demo_mode"]) != 1:
            raise HTTPException(400, "Demo automation is available only for demo sessions.")
        if int(sess["locked"]) != 1:
            raise HTTPException(400, "Demo session must be grouped before auto-submitting actions.")
        if int(sess["action_open"]) != 1:
            raise HTTPException(400, "Action stage must be open before auto-submitting demo actions.")

        phase, _ = phase_for_round(round_no)
        if phase not in {"reward", "punishment"}:
            raise HTTPException(400, "Baseline rounds do not have reward or punishment actions.")

        students = conn.execute(
            """
            SELECT id, student_id, group_no
            FROM students
            WHERE session_id=? AND group_no IS NOT NULL
            ORDER BY group_no ASC, group_pos ASC, joined_at ASC
        """,
            (session_id,),
        ).fetchall()
        groups: Dict[int, List[sqlite3.Row]] = {}
        for student in students:
            groups.setdefault(int(student["group_no"]), []).append(student)

        contrib_rows = conn.execute(
            "SELECT student_id, contrib FROM contributions WHERE session_id=? AND round_no=?",
            (session_id, round_no),
        ).fetchall()
        contrib_by_student = {str(row["student_id"]): int(row["contrib"]) for row in contrib_rows}
        existing_actor_ids = {
            str(row["actor_student_id"])
            for row in conn.execute(
                "SELECT DISTINCT actor_student_id FROM actions WHERE session_id=? AND round_no=?",
                (session_id, round_no),
            ).fetchall()
        }

        rows_to_insert = []
        actors_filled = 0
        for members in groups.values():
            group_avg = sum(contrib_by_student.get(str(member["id"]), 0) for member in members) / max(1, len(members))
            max_contrib = max((contrib_by_student.get(str(member["id"]), 0) for member in members), default=0)
            min_contrib = min((contrib_by_student.get(str(member["id"]), 0) for member in members), default=0)
            for actor in members:
                actor_id = str(actor["id"])
                if actor_id in existing_actor_ids:
                    continue
                actor_public_id = str(actor["student_id"])
                actor_profile = _demo_profile_for_student(actor_public_id)
                actor_contrib = contrib_by_student.get(actor_id, 0)
                actor_rows: List[Tuple[str, str, str, str, int, str]] = []
                for target in members:
                    target_id = str(target["id"])
                    if target_id == actor_id:
                        continue
                    target_contrib = contrib_by_student.get(target_id, 0)
                    points = _demo_action_points(
                        phase,
                        actor_profile,
                        actor_contrib,
                        target_contrib,
                        group_avg,
                        target_contrib == max_contrib,
                    )
                    points = max(0, min(MAX_ACTION_POINTS, points))
                    if points <= 0:
                        continue
                    actor_rows.append((session_id, round_no, actor_id, target_id, points, now_iso()))

                if not actor_rows:
                    fallback_target = None
                    if phase == "reward":
                        fallback_target = next(
                            (
                                target
                                for target in members
                                if str(target["id"]) != actor_id
                                and contrib_by_student.get(str(target["id"]), 0) == max_contrib
                            ),
                            None,
                        )
                    else:
                        fallback_target = next(
                            (
                                target
                                for target in members
                                if str(target["id"]) != actor_id
                                and contrib_by_student.get(str(target["id"]), 0) == min_contrib
                            ),
                            None,
                        )
                    if fallback_target is not None:
                        actor_rows.append(
                            (
                                session_id,
                                round_no,
                                actor_id,
                                str(fallback_target["id"]),
                                1,
                                now_iso(),
                            )
                        )

                rows_to_insert.extend(actor_rows)
                actors_filled += 1

        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
                VALUES(?,?,?,?,?,?)
            """,
                rows_to_insert,
            )
        conn.commit()
        return actors_filled
    finally:
        conn.close()


def simulate_demo_current_round(session_id: str) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Demo session must be grouped before auto-running rounds.")

    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    phase, _ = phase_for_round(round_no)
    stage = stage_of_session(sess)

    if stage == "closed":
        open_round(session_id, round_no)
        stage = "contribution"

    contrib_filled = 0
    if stage == "contribution":
        contrib_filled = simulate_demo_contributions(session_id, round_no)
        if phase == "baseline":
            close_round(session_id)
            compute_results(session_id, round_no)
            advance_round(session_id, round_no, rounds)
            return {
                "round": round_no,
                "phase": phase,
                "contrib_filled": contrib_filled,
                "actions_filled": 0,
            }
        open_action_stage(session_id)
        stage = "action"

    if stage != "action":
        raise HTTPException(400, "Unable to advance the session automatically from the current stage.")

    actions_filled = simulate_demo_actions(session_id, round_no)
    close_round(session_id)
    compute_results(session_id, round_no)
    advance_round(session_id, round_no, rounds)
    return {
        "round": round_no,
        "phase": phase,
        "contrib_filled": contrib_filled,
        "actions_filled": actions_filled,
    }


def simulate_demo_current_phase(session_id: str) -> Dict[str, object]:
    sess = get_session(session_id)
    if int(sess["demo_mode"]) != 1:
        raise HTTPException(400, "Demo automation is available only for demo sessions.")

    rounds = int(sess["rounds"])
    current_phase, _ = phase_for_round(int(sess["current_round"]))
    phase_bounds = phase_round_bounds_for_session(rounds, current_phase)
    if phase_bounds is None:
        raise HTTPException(400, "Current phase is unavailable for this session.")

    conn = db()
    try:
        counts = phase_computed_counts_conn(conn, session_id)
    finally:
        conn.close()
    target_total = phase_round_count_for_session(rounds, current_phase)

    simulated_rounds = 0
    max_iterations = target_total + 2
    for _ in range(max_iterations):
        conn = db()
        try:
            counts = phase_computed_counts_conn(conn, session_id)
        finally:
            conn.close()
        if int(counts.get(current_phase, 0)) >= target_total:
            break
        simulate_demo_current_round(session_id)
        simulated_rounds += 1

    conn = db()
    try:
        counts = phase_computed_counts_conn(conn, session_id)
    finally:
        conn.close()
    if int(counts.get(current_phase, 0)) < target_total:
        raise HTTPException(500, "Demo autoplay did not finish the current phase.")

    return {
        "phase": current_phase,
        "phase_label": phase_label(current_phase),
        "rounds_simulated": simulated_rounds,
    }
