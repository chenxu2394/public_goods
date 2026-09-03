from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        username_norm TEXT NOT NULL,
        email TEXT,
        email_norm TEXT,
        identity_provider TEXT,
        identity_subject TEXT,
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
        current_phase TEXT,
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


def create_session_join_token_index(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_join_token ON sessions(join_token)")


def create_user_identity_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_norm
        ON users(email_norm)
        WHERE email_norm IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_identity
        ON users(identity_provider, identity_subject)
        WHERE identity_provider IS NOT NULL AND identity_subject IS NOT NULL
        """
    )
