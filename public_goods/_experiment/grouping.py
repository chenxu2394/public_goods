from __future__ import annotations

import math
import random
from typing import List, Optional, Tuple

from ..config import DEFAULT_GROUP_SIZE, MAX_GROUP_SIZE, MIN_GROUP_SIZE
from ..db import db


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
