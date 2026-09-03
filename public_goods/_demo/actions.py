from __future__ import annotations

import sqlite3
from typing import Dict, List, Tuple

from fastapi import HTTPException

from ..config import MAX_ACTION_POINTS
from ..db import db, now_iso
from .._experiment import selected_phase
from .utils import _demo_profile_for_student


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

        phase = selected_phase(sess)
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
