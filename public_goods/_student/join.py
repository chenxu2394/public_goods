from __future__ import annotations

from fastapi import HTTPException

from .._sessions import get_session_by_join_token, upsert_joined_student, whitelist_check_or_raise
from ..experiment import assign_late_joiner


def submit_student_join(join_token: str, student_id: str, name: str) -> tuple[str, str]:
    sess = get_session_by_join_token(join_token)
    session_id = str(sess["id"])
    student_id = student_id.strip()
    name = name.strip()
    if not student_id or not name:
        raise HTTPException(400, "student_id and name required")

    whitelist_check_or_raise(session_id, student_id, name)
    upsert_joined_student(session_id, student_id, name)

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    return session_id, student_id
