from __future__ import annotations

import sqlite3
from typing import Dict, List


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
               COUNT(DISTINCT CASE
                   WHEN c.action_submitted_at IS NOT NULL OR a.actor_student_id IS NOT NULL
                   THEN s.id
               END) AS action_submitted
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
