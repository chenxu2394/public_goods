from __future__ import annotations

from .connection import _configure_sqlite_storage, db
from .identifiers import _backfill_anonymous_ids
from .migrations import backfill_result_phase_fields, apply_schema_migrations
from .schema import create_schema, create_session_join_token_index


def init_db() -> None:
    from .._sessions.users import _backfill_session_join_tokens, _backfill_session_owners, _ensure_bootstrap_admin

    conn = db()
    try:
        _configure_sqlite_storage(conn)
        create_schema(conn)
        apply_schema_migrations(conn)
        backfill_result_phase_fields(conn)

        admin_user = _ensure_bootstrap_admin(conn)
        if admin_user is not None:
            _backfill_session_owners(conn, admin_user["id"])

        _backfill_session_join_tokens(conn)
        create_session_join_token_index(conn)
        _backfill_anonymous_ids(conn)

        conn.commit()
    finally:
        conn.close()

