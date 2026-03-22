from __future__ import annotations

import sqlite3

from ..db import db
from .duplication_copy import (
    _copy_whitelist_conn,
    _duplicate_session_conn,
    _duplicate_session_setup_conn,
    _get_source_session_and_admin_conn,
    _insert_admin_owned_session_copy_conn,
)


def duplicate_session_as_admin(session_id: str) -> str:
    conn = db()
    try:
        conn.execute("BEGIN")
        duplicate_session_id = _duplicate_session_conn(conn, session_id)
        conn.commit()
        return duplicate_session_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def duplicate_session_setup_as_admin(session_id: str) -> str:
    conn = db()
    try:
        conn.execute("BEGIN")
        duplicate_session_id = _duplicate_session_setup_conn(conn, session_id)
        conn.commit()
        return duplicate_session_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
