from __future__ import annotations

from .connection import _configure_sqlite_storage, db
from .identifiers import _backfill_anonymous_ids
from .migrations import apply_schema_migrations, backfill_active_session_phases, backfill_result_phase_fields
from .schema import create_schema, create_session_join_token_index, create_user_identity_indexes


def init_db() -> None:
    from .._sessions import _backfill_session_join_tokens, _backfill_session_owners, _ensure_bootstrap_admin

    conn = db()
    try:
        _configure_sqlite_storage(conn)
        create_schema(conn)
        apply_schema_migrations(conn)
        create_user_identity_indexes(conn)
        backfill_result_phase_fields(conn)
        backfill_active_session_phases(conn)

        admin_user = _ensure_bootstrap_admin(conn)
        if admin_user is not None:
            _backfill_session_owners(conn, admin_user["id"])

        _backfill_session_join_tokens(conn)
        create_session_join_token_index(conn)
        _backfill_anonymous_ids(conn)

        conn.commit()
    finally:
        conn.close()
