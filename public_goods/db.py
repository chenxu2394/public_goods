from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
import time
from typing import Callable, Dict, Optional, Tuple, TypeVar

from .config import (
    DB_PATH,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_JOURNAL_MODE,
    SQLITE_WRITE_RETRY_ATTEMPTS,
    SQLITE_WRITE_RETRY_BASE_DELAY_MS,
)


T = TypeVar("T")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _configure_sqlite_storage(conn: sqlite3.Connection) -> None:
    if not SQLITE_JOURNAL_MODE:
        return
    try:
        conn.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}").fetchone()
    except sqlite3.DatabaseError:
        return


def _is_locked_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database schema is locked" in msg


def _run_write_with_retry(work: Callable[[sqlite3.Connection], T]) -> T:
    attempt = 0
    while True:
        attempt += 1
        conn = db()
        try:
            result = work(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if attempt >= SQLITE_WRITE_RETRY_ATTEMPTS or not _is_locked_sqlite_error(exc):
                raise
            if SQLITE_WRITE_RETRY_BASE_DELAY_MS > 0:
                time.sleep((SQLITE_WRITE_RETRY_BASE_DELAY_MS / 1000.0) * attempt)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


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


def _generate_anonymous_id(existing: set[str]) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"

    for _ in range(1000):
        code = f"{secrets.choice(letters)}{secrets.choice(digits)}"
        if code not in existing:
            return code

    while True:
        code = f"{secrets.choice(letters)}{secrets.choice(digits)}{secrets.choice(letters)}"
        if code not in existing:
            return code


def _backfill_anonymous_ids(conn: sqlite3.Connection) -> None:
    sessions = conn.execute("SELECT DISTINCT session_id FROM students").fetchall()
    for session_row in sessions:
        session_id = session_row["session_id"]
        students = conn.execute(
            "SELECT id, anonymous_id FROM students WHERE session_id=? ORDER BY joined_at ASC",
            (session_id,),
        ).fetchall()
        used = {s["anonymous_id"] for s in students if s["anonymous_id"]}
        updates = []
        for student in students:
            if student["anonymous_id"]:
                continue
            anonymous_id = _generate_anonymous_id(used)
            used.add(anonymous_id)
            updates.append((anonymous_id, student["id"]))
        if updates:
            conn.executemany("UPDATE students SET anonymous_id=? WHERE id=?", updates)


def _get_setting_conn(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_setting(key: str) -> Optional[str]:
    conn = db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = db()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _generate_unique_token_conn(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    nbytes: int,
    reserved: Optional[set[str]] = None,
) -> str:
    reserved = reserved or set()
    for _ in range(100):
        token = secrets.token_urlsafe(nbytes)
        if token in reserved:
            continue
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (token,)).fetchone()
        if row is None:
            return token
    raise RuntimeError(f"Failed to generate a unique token for {table}.{column}")


def init_db() -> None:
    from .experiment import phase_for_round
    from .sessions import _backfill_session_join_tokens, _backfill_session_owners, _ensure_bootstrap_admin

    conn = db()
    _configure_sqlite_storage(conn)
    cur = conn.cursor()

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        username_norm TEXT NOT NULL,
        role TEXT NOT NULL,
        password_scheme TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        password_version INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        disabled_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """
    )
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_norm ON users(username_norm)")

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        group_size INTEGER NOT NULL,
        multiplier REAL NOT NULL,
        endowment INTEGER NOT NULL,
        rounds INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        current_round INTEGER NOT NULL DEFAULT 1,
        round_open INTEGER NOT NULL DEFAULT 0,
        action_open INTEGER NOT NULL DEFAULT 0,
        demo_mode INTEGER NOT NULL DEFAULT 0,
        join_token TEXT
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS students(
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        name TEXT NOT NULL,
        anonymous_id TEXT,
        joined_at TEXT NOT NULL,
        group_no INTEGER,
        group_pos INTEGER,
        UNIQUE(session_id, student_id)
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS whitelist(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        name TEXT NOT NULL,
        added_at TEXT NOT NULL,
        UNIQUE(session_id, student_id)
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS contributions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        round_no INTEGER NOT NULL,
        student_id TEXT NOT NULL,
        contrib INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, round_no, student_id)
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        round_no INTEGER NOT NULL,
        actor_student_id TEXT NOT NULL,
        target_student_id TEXT NOT NULL,
        points INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, round_no, actor_student_id, target_student_id)
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS results(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        round_no INTEGER NOT NULL,
        student_id TEXT NOT NULL,
        group_no INTEGER NOT NULL,
        group_n INTEGER NOT NULL,
        group_total INTEGER NOT NULL,
        public_return REAL NOT NULL,
        contrib INTEGER NOT NULL DEFAULT 0,
        phase TEXT NOT NULL DEFAULT 'baseline',
        phase_round INTEGER NOT NULL DEFAULT 1,
        action_sent INTEGER NOT NULL DEFAULT 0,
        action_received INTEGER NOT NULL DEFAULT 0,
        action_cost REAL NOT NULL DEFAULT 0,
        action_effect REAL NOT NULL DEFAULT 0,
        income REAL NOT NULL,
        cumulative REAL NOT NULL,
        phase_cumulative REAL NOT NULL DEFAULT 0,
        computed_at TEXT NOT NULL,
        UNIQUE(session_id, round_no, student_id)
    )
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """
    )

    _migrate_student_identifier_columns(conn)

    _ensure_column(conn, "sessions", "action_open", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sessions", "demo_mode", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sessions", "join_token", "TEXT")
    _ensure_column(conn, "sessions", "owner_user_id", "TEXT")
    _ensure_column(conn, "sessions", "teacher_removed_at", "TEXT")
    _ensure_column(conn, "sessions", "teacher_removed_by_user_id", "TEXT")
    _ensure_column(conn, "students", "anonymous_id", "TEXT")

    _ensure_column(conn, "results", "contrib", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "phase", "TEXT NOT NULL DEFAULT 'baseline'")
    _ensure_column(conn, "results", "phase_round", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "results", "action_sent", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_received", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_cost", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_effect", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "phase_cumulative", "REAL NOT NULL DEFAULT 0")

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

    admin_user = _ensure_bootstrap_admin(conn)
    if admin_user is not None:
        _backfill_session_owners(conn, admin_user["id"])

    _backfill_session_join_tokens(conn)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_join_token ON sessions(join_token)")
    _backfill_anonymous_ids(conn)

    conn.commit()
    conn.close()
