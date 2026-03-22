from __future__ import annotations

import datetime as dt
import secrets

from fastapi import HTTPException

from ..config import DEFAULT_GROUP_SIZE
from ..db import _generate_anonymous_id, db
from .._experiment import lock_groups
from .utils import _demo_student_rows


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
