from __future__ import annotations

import sqlite3
from typing import Dict, Tuple

from fastapi import HTTPException

from ..db import _generate_unique_token_conn, now_iso
from .bootstrap import _get_admin_user_conn


def _get_source_session_and_admin_conn(conn: sqlite3.Connection, session_id: str) -> Tuple[sqlite3.Row, sqlite3.Row]:
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        raise HTTPException(404, "Session not found")

    admin_user = _get_admin_user_conn(conn)
    if not admin_user:
        raise HTTPException(500, "Admin account is unavailable.")

    return sess, admin_user


def _insert_admin_owned_session_copy_conn(
    conn: sqlite3.Connection,
    sess: sqlite3.Row,
    admin_user_id: str,
    *,
    title_suffix: str,
    locked: int,
    current_round: int,
    current_phase: str | None,
    round_open: int,
    action_open: int,
) -> str:
    new_session_id = _generate_unique_token_conn(conn, "sessions", "id", nbytes=6)
    join_token = _generate_unique_token_conn(
        conn,
        "sessions",
        "join_token",
        nbytes=6,
        reserved={new_session_id, str(sess["id"])},
    )
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, group_size, multiplier, endowment, rounds, created_at,
            locked, current_round, current_phase, round_open, action_open, join_token,
            owner_user_id, teacher_removed_at, teacher_removed_by_user_id
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            new_session_id,
            f"{sess['title']}{title_suffix}",
            int(sess["group_size"]),
            float(sess["multiplier"]),
            int(sess["endowment"]),
            int(sess["rounds"]),
            created_at,
            int(locked),
            int(current_round),
            current_phase,
            int(round_open),
            int(action_open),
            join_token,
            admin_user_id,
            None,
            None,
        ),
    )
    return new_session_id


def _copy_whitelist_conn(conn: sqlite3.Connection, source_session_id: str, target_session_id: str) -> None:
    whitelist_rows = conn.execute(
        """
        SELECT student_id, name, added_at
        FROM whitelist
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (source_session_id,),
    ).fetchall()
    if whitelist_rows:
        conn.executemany(
            """
            INSERT INTO whitelist(session_id, student_id, name, added_at)
            VALUES(?,?,?,?)
        """,
            [(target_session_id, row["student_id"], row["name"], row["added_at"]) for row in whitelist_rows],
        )


def _duplicate_session_conn(conn: sqlite3.Connection, session_id: str) -> str:
    sess, admin_user = _get_source_session_and_admin_conn(conn, session_id)
    new_session_id = _insert_admin_owned_session_copy_conn(
        conn,
        sess,
        str(admin_user["id"]),
        title_suffix=" (Copy)",
        locked=int(sess["locked"]),
        current_round=int(sess["current_round"]),
        current_phase=sess["current_phase"],
        round_open=int(sess["round_open"]),
        action_open=int(sess["action_open"]),
    )

    _copy_whitelist_conn(conn, session_id, new_session_id)

    students = conn.execute(
        """
        SELECT id, student_id, name, anonymous_id, joined_at, group_no, group_pos
        FROM students
        WHERE session_id=?
        ORDER BY joined_at ASC, id ASC
    """,
        (session_id,),
    ).fetchall()
    student_id_map: Dict[str, str] = {}
    reserved_student_ids: set[str] = set()
    if students:
        student_rows = []
        for student in students:
            new_student_id = _generate_unique_token_conn(
                conn,
                "students",
                "id",
                nbytes=8,
                reserved=reserved_student_ids,
            )
            reserved_student_ids.add(new_student_id)
            student_id_map[str(student["id"])] = new_student_id
            student_rows.append(
                (
                    new_student_id,
                    new_session_id,
                    student["student_id"],
                    student["name"],
                    student["anonymous_id"],
                    student["joined_at"],
                    student["group_no"],
                    student["group_pos"],
                )
            )
        conn.executemany(
            """
            INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            student_rows,
        )

    def remap_student_id(source_student_id: str) -> str:
        mapped = student_id_map.get(str(source_student_id))
        if not mapped:
            raise RuntimeError(f"Missing duplicated student mapping for session {session_id}")
        return mapped

    contribution_rows = conn.execute(
        """
        SELECT round_no, student_id, contrib, created_at
        FROM contributions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if contribution_rows:
        conn.executemany(
            """
            INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
            VALUES(?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["student_id"])),
                    int(row["contrib"]),
                    row["created_at"],
                )
                for row in contribution_rows
            ],
        )

    action_rows = conn.execute(
        """
        SELECT round_no, actor_student_id, target_student_id, points, created_at
        FROM actions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if action_rows:
        conn.executemany(
            """
            INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
            VALUES(?,?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["actor_student_id"])),
                    remap_student_id(str(row["target_student_id"])),
                    int(row["points"]),
                    row["created_at"],
                )
                for row in action_rows
            ],
        )

    result_rows = conn.execute(
        """
        SELECT round_no, student_id, group_no, group_n, group_total, public_return,
               contrib, phase, phase_round, action_sent, action_received,
               action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
        FROM results
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if result_rows:
        conn.executemany(
            """
            INSERT INTO results(
                session_id, round_no, student_id, group_no, group_n, group_total,
                public_return, contrib, phase, phase_round, action_sent, action_received,
                action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["student_id"])),
                    int(row["group_no"]),
                    int(row["group_n"]),
                    int(row["group_total"]),
                    float(row["public_return"]),
                    int(row["contrib"]),
                    row["phase"],
                    int(row["phase_round"]),
                    int(row["action_sent"]),
                    int(row["action_received"]),
                    float(row["action_cost"]),
                    float(row["action_effect"]),
                    float(row["income"]),
                    float(row["cumulative"]),
                    float(row["phase_cumulative"]),
                    row["computed_at"],
                )
                for row in result_rows
            ],
        )

    return new_session_id


def _duplicate_session_setup_conn(conn: sqlite3.Connection, session_id: str) -> str:
    sess, admin_user = _get_source_session_and_admin_conn(conn, session_id)
    new_session_id = _insert_admin_owned_session_copy_conn(
        conn,
        sess,
        str(admin_user["id"]),
        title_suffix=" (Setup Copy)",
        locked=0,
        current_round=1,
        current_phase=None,
        round_open=0,
        action_open=0,
    )
    _copy_whitelist_conn(conn, session_id, new_session_id)
    return new_session_id
