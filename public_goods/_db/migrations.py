from __future__ import annotations

import sqlite3
from typing import Dict, Tuple

from .._experiment.phases import phase_for_round


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, col_name: str, col_type_expr: str) -> None:
    cols = _column_names(conn, table)
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type_expr}")


def _migrate_student_identifier_columns(conn: sqlite3.Connection) -> None:
    for table in ("students", "whitelist"):
        cols = _column_names(conn, table)
        if "student_code" in cols and "student_id" not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN student_code TO student_id")


def apply_schema_migrations(conn: sqlite3.Connection) -> None:
    _migrate_student_identifier_columns(conn)

    _ensure_column(conn, "users", "email", "TEXT")
    _ensure_column(conn, "users", "email_norm", "TEXT")
    _ensure_column(conn, "users", "identity_provider", "TEXT")
    _ensure_column(conn, "users", "identity_subject", "TEXT")

    _ensure_column(conn, "sessions", "action_open", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sessions", "current_phase", "TEXT")
    _ensure_column(conn, "sessions", "demo_mode", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sessions", "join_token", "TEXT")
    _ensure_column(conn, "sessions", "join_link_enabled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "sessions", "owner_user_id", "TEXT")
    _ensure_column(conn, "sessions", "teacher_removed_at", "TEXT")
    _ensure_column(conn, "sessions", "teacher_removed_by_user_id", "TEXT")
    _ensure_column(conn, "students", "anonymous_id", "TEXT")
    _ensure_column(conn, "contributions", "action_submitted_at", "TEXT")

    _ensure_column(conn, "results", "contrib", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "phase", "TEXT NOT NULL DEFAULT 'baseline'")
    _ensure_column(conn, "results", "phase_round", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "results", "action_sent", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_received", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_cost", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_effect", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "phase_cumulative", "REAL NOT NULL DEFAULT 0")


def backfill_result_phase_fields(conn: sqlite3.Connection) -> None:
    migration_key = "result_phase_fields_backfilled_v1"
    already_backfilled = conn.execute(
        "SELECT 1 FROM settings WHERE key=?",
        (migration_key,),
    ).fetchone()
    if already_backfilled:
        return

    result_rows = conn.execute(
        """
        SELECT id, session_id, student_id, round_no, income
        FROM results
        ORDER BY session_id ASC, student_id ASC, round_no ASC, id ASC
    """
    ).fetchall()
    phase_running: Dict[Tuple[str, str, str], float] = {}
    phase_updates = []
    for row in result_rows:
        round_no = int(row["round_no"])
        phase, phase_round = phase_for_round(round_no)
        key = (str(row["session_id"]), str(row["student_id"]), phase)
        phase_cumulative = phase_running.get(key, 0.0) + float(row["income"])
        phase_running[key] = phase_cumulative
        phase_updates.append((phase, phase_round, phase_cumulative, row["id"]))
    if phase_updates:
        conn.executemany(
            """
            UPDATE results
            SET phase=?, phase_round=?, phase_cumulative=?
            WHERE id=?
        """,
            phase_updates,
        )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, '1')",
        (migration_key,),
    )


def backfill_active_session_phases(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, current_round, round_open, action_open
        FROM sessions
        WHERE current_phase IS NULL
    """
    ).fetchall()
    updates = []
    for row in rows:
        result = conn.execute(
            """
            SELECT phase
            FROM results
            WHERE session_id=? AND round_no=?
            LIMIT 1
        """,
            (row["id"], row["current_round"]),
        ).fetchone()
        if result is not None:
            updates.append((result["phase"], row["id"]))
        elif int(row["round_open"]) == 1 or int(row["action_open"]) == 1:
            phase, _ = phase_for_round(int(row["current_round"]))
            updates.append((phase, row["id"]))
    if updates:
        conn.executemany("UPDATE sessions SET current_phase=? WHERE id=?", updates)
