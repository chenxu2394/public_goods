from __future__ import annotations

import os
import sqlite3
import secrets
import datetime as dt
import csv
import io
import base64
import json
import hmac
import hashlib
import random
import math
from typing import Any, List, Dict, Tuple, Optional

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import segno


APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Azure Web App: /home is persistent
DEFAULT_DB_PATH = "/home/public_goods.db"
DB_PATH = os.environ.get("PUBLIC_GOODS_DB_PATH", DEFAULT_DB_PATH)

# Auth / account protection
ADMIN_PASSWORD = (os.environ.get("ADMIN_PASSWORD") or "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()  # used to sign auth cookie tokens
AUTH_COOKIE_NAME = "pg_auth"
LEGACY_ADMIN_COOKIE_NAME = "pg_admin"
AUTH_TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
USER_ROLE_ADMIN = "admin"
USER_ROLE_TEACHER = "teacher"
PASSWORD_SCHEME_PBKDF2 = "pbkdf2_sha256_v1"
PASSWORD_SCHEME_LEGACY_ADMIN = "legacy_admin_secretkey_v1"
PASSWORD_HASH_ITERATIONS = 200_000

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://public-goods.azurewebsites.net").rstrip("/")

PHASE_ROUNDS = 10
PHASES = ("baseline", "reward", "punishment")
PHASE_LABELS = {
    "baseline": "Baseline",
    "reward": "Reward",
    "punishment": "Punishment",
}
TOTAL_EXPERIMENT_ROUNDS = PHASE_ROUNDS * len(PHASES)
MIN_GROUP_SIZE = 3
MAX_GROUP_SIZE = 7
DEFAULT_GROUP_SIZE = 5

ACTION_COST = 1.0
REWARD_EFFECT = 2.0
PUNISH_EFFECT = 3.0
MAX_ACTION_POINTS = 5

app = FastAPI(title="Public Goods Experiment (Azure + Multi-User Auth)")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, col_name: str, col_type_expr: str) -> None:
    cols = _column_names(conn, table)
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type_expr}")


def _migrate_student_identifier_columns(conn: sqlite3.Connection) -> None:
    # Backward compatibility for existing DBs that still use student_code.
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
        for s in students:
            if s["anonymous_id"]:
                continue
            aid = _generate_anonymous_id(used)
            used.add(aid)
            updates.append((aid, s["id"]))
        if updates:
            conn.executemany("UPDATE students SET anonymous_id=? WHERE id=?", updates)


def _normalize_username(username: str) -> str:
    return username.strip().casefold()


def _validate_username(username: str) -> str:
    value = username.strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if len(value) < 3 or len(value) > 32:
        raise HTTPException(400, "Username must be 3-32 characters.")
    if any(ch not in allowed for ch in value):
        raise HTTPException(400, "Username may contain only letters, numbers, '.', '_' or '-'.")
    return value


def _validate_session_title(title: str) -> str:
    value = title.strip()
    if not value:
        raise HTTPException(400, "Session title must not be empty.")
    if len(value) > 200:
        raise HTTPException(400, "Session title must be at most 200 characters.")
    return value


def _generate_password_salt() -> str:
    return _b64url(secrets.token_bytes(16))


def _hash_password_with_salt(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _b64url_decode(salt),
        PASSWORD_HASH_ITERATIONS,
    )
    return _b64url(dk)


def _hash_password_record(password: str) -> Tuple[str, str]:
    salt = _generate_password_salt()
    return _hash_password_with_salt(password, salt), salt


def _legacy_hash_password(password: str) -> str:
    if not SECRET_KEY:
        return ""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        SECRET_KEY.encode("utf-8"),
        100_000,
    )
    return _b64url(dk)


def _get_setting_conn(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _create_user_conn(
    conn: sqlite3.Connection,
    username: str,
    role: str,
    *,
    password: Optional[str] = None,
    password_scheme: Optional[str] = None,
    password_hash: Optional[str] = None,
    password_salt: Optional[str] = None,
    password_version: int = 1,
    must_change_password: bool = False,
) -> sqlite3.Row:
    if role not in {USER_ROLE_ADMIN, USER_ROLE_TEACHER}:
        raise ValueError("Invalid user role")
    username = _validate_username(username)
    username_norm = _normalize_username(username)
    if password is not None:
        password_hash, password_salt = _hash_password_record(password)
        password_scheme = PASSWORD_SCHEME_PBKDF2
    if not password_scheme or password_hash is None or password_salt is None:
        raise ValueError("Password material is required")

    now = now_iso()
    user_id = secrets.token_urlsafe(8)
    conn.execute(
        """
        INSERT INTO users(
            id, username, username_norm, role, password_scheme, password_hash, password_salt,
            password_version, must_change_password, disabled_at, created_at, updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            user_id,
            username,
            username_norm,
            role,
            password_scheme,
            password_hash,
            password_salt,
            int(password_version),
            1 if must_change_password else 0,
            None,
            now,
            now,
        ),
    )
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def _set_user_password_conn(
    conn: sqlite3.Connection,
    user_id: str,
    new_password: str,
    *,
    must_change_password: bool,
) -> None:
    password_hash, password_salt = _hash_password_record(new_password)
    conn.execute(
        """
        UPDATE users
        SET password_scheme=?, password_hash=?, password_salt=?,
            password_version=password_version + 1,
            must_change_password=?, updated_at=?
        WHERE id=?
    """,
        (
            PASSWORD_SCHEME_PBKDF2,
            password_hash,
            password_salt,
            1 if must_change_password else 0,
            now_iso(),
            user_id,
        ),
    )


def _rehash_legacy_user_password_conn(conn: sqlite3.Connection, user_id: str, entered_password: str) -> None:
    password_hash, password_salt = _hash_password_record(entered_password)
    conn.execute(
        """
        UPDATE users
        SET password_scheme=?, password_hash=?, password_salt=?, updated_at=?
        WHERE id=?
    """,
        (
            PASSWORD_SCHEME_PBKDF2,
            password_hash,
            password_salt,
            now_iso(),
            user_id,
        ),
    )
    conn.execute("DELETE FROM settings WHERE key IN ('admin_password_hash', 'password_changed_at')")


def _ensure_bootstrap_admin(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    admin = conn.execute(
        "SELECT * FROM users WHERE username_norm=?",
        (_normalize_username("admin"),),
    ).fetchone()
    if admin:
        return admin

    legacy_hash = (_get_setting_conn(conn, "admin_password_hash") or "").strip()
    if legacy_hash:
        return _create_user_conn(
            conn,
            "admin",
            USER_ROLE_ADMIN,
            password_scheme=PASSWORD_SCHEME_LEGACY_ADMIN,
            password_hash=legacy_hash,
            password_salt="",
        )

    if ADMIN_PASSWORD:
        return _create_user_conn(conn, "admin", USER_ROLE_ADMIN, password=ADMIN_PASSWORD)

    return None


def _backfill_session_owners(conn: sqlite3.Connection, admin_user_id: str) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET owner_user_id=?
        WHERE owner_user_id IS NULL OR TRIM(owner_user_id)=''
    """,
        (admin_user_id,),
    )


def _backfill_session_join_tokens(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET join_token=id
        WHERE join_token IS NULL OR TRIM(join_token)=''
    """
    )


def _get_admin_user_conn(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE username_norm=?",
        (_normalize_username("admin"),),
    ).fetchone()


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


def phase_for_round(round_no: int) -> Tuple[str, int]:
    if round_no <= PHASE_ROUNDS:
        return "baseline", round_no
    if round_no <= PHASE_ROUNDS * 2:
        return "reward", round_no - PHASE_ROUNDS
    return "punishment", max(1, round_no - PHASE_ROUNDS * 2)


def phase_start_round(phase: str) -> int:
    if phase == "baseline":
        return 1
    if phase == "reward":
        return PHASE_ROUNDS + 1
    if phase == "punishment":
        return PHASE_ROUNDS * 2 + 1
    raise HTTPException(400, "Invalid phase")


def phase_round_count_for_session(total_rounds: int, phase: str) -> int:
    start = phase_start_round(phase)
    if total_rounds < start:
        return 0
    return min(PHASE_ROUNDS, total_rounds - start + 1)


def phase_round_bounds_for_session(total_rounds: int, phase: str) -> Optional[Tuple[int, int]]:
    phase_rounds = phase_round_count_for_session(total_rounds, phase)
    if phase_rounds <= 0:
        return None
    start = phase_start_round(phase)
    return start, start + phase_rounds - 1


def stage_of_session(sess: sqlite3.Row) -> str:
    if int(sess["action_open"]) == 1:
        return "action"
    if int(sess["round_open"]) == 1:
        return "contribution"
    return "closed"


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase.title())


def round_context(sess: sqlite3.Row) -> Dict[str, object]:
    cur = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur)
    stage = stage_of_session(sess)

    if stage == "contribution":
        if phase == "baseline":
            close_label = "Close contribution and compute round"
        else:
            close_label = f"Close contribution and open {phase_label(phase)} stage"
    elif stage == "action":
        close_label = f"Close {phase_label(phase)} stage and compute round"
    else:
        close_label = "Round is closed (open current round first)"

    return {
        "round": cur,
        "phase": phase,
        "phase_label": phase_label(phase),
        "phase_round": phase_round,
        "stage": stage,
        "close_label": close_label,
    }


def qr_svg_data_uri(content: str) -> str:
    qr = segno.make_qr(content, error="m")
    output = io.BytesIO()
    qr.save(output, kind="svg", scale=6, border=2, dark="#111111", light="#ffffff")
    svg = output.getvalue()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def init_db():
    conn = db()
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

    # Whitelist (per session)
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


# ---------------- Auth ----------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(message: bytes) -> str:
    mac = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    return _b64url(mac)


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


def get_user_by_id(user_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    username_norm = _normalize_username(username)
    conn = db()
    try:
        return conn.execute("SELECT * FROM users WHERE username_norm=?", (username_norm,)).fetchone()
    finally:
        conn.close()


def list_teachers(*, active_only: bool = False) -> List[sqlite3.Row]:
    conn = db()
    try:
        if active_only:
            return conn.execute(
                """
                SELECT *
                FROM users
                WHERE role=? AND disabled_at IS NULL
                ORDER BY username_norm ASC
            """,
                (USER_ROLE_TEACHER,),
            ).fetchall()
        return conn.execute(
            """
            SELECT *
            FROM users
            WHERE role=?
            ORDER BY username_norm ASC
        """,
            (USER_ROLE_TEACHER,),
        ).fetchall()
    finally:
        conn.close()


def create_user(username: str, role: str, password: str, *, must_change_password: bool) -> sqlite3.Row:
    conn = db()
    try:
        user = _create_user_conn(
            conn,
            username,
            role,
            password=password,
            must_change_password=must_change_password,
        )
        conn.commit()
        return user
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_password(user_id: str, new_password: str, *, must_change_password: bool) -> None:
    conn = db()
    try:
        _set_user_password_conn(
            conn,
            user_id,
            new_password,
            must_change_password=must_change_password,
        )
        conn.commit()
    finally:
        conn.close()


def set_user_disabled(user_id: str, disabled: bool) -> None:
    conn = db()
    try:
        conn.execute(
            "UPDATE users SET disabled_at=?, updated_at=? WHERE id=?",
            (now_iso() if disabled else None, now_iso(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def transfer_session_owner(session_id: str, owner_user_id: str) -> None:
    conn = db()
    try:
        conn.execute(
            """
            UPDATE sessions
            SET owner_user_id=?,
                teacher_removed_at=NULL,
                teacher_removed_by_user_id=NULL
            WHERE id=?
        """,
            (owner_user_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def archive_session_to_admin(session_id: str, removed_by_user_id: str) -> None:
    conn = db()
    try:
        admin_user = _get_admin_user_conn(conn)
        if not admin_user:
            raise HTTPException(500, "Admin account is unavailable.")
        conn.execute(
            """
            UPDATE sessions
            SET owner_user_id=?,
                teacher_removed_at=?,
                teacher_removed_by_user_id=?
            WHERE id=?
        """,
            (str(admin_user["id"]), now_iso(), removed_by_user_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def rotate_session_join_token(session_id: str) -> str:
    conn = db()
    try:
        sess = conn.execute("SELECT join_token FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(404, "Session not found")
        new_join_token = _generate_unique_token_conn(
            conn,
            "sessions",
            "join_token",
            nbytes=6,
            reserved={str(sess["join_token"] or ""), str(session_id)},
        )
        conn.execute("UPDATE sessions SET join_token=? WHERE id=?", (new_join_token, session_id))
        conn.commit()
        return new_join_token
    finally:
        conn.close()


def set_session_title(session_id: str, title: str) -> None:
    conn = db()
    try:
        conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
        conn.commit()
    finally:
        conn.close()


def _get_source_session_and_admin_conn(conn: sqlite3.Connection, session_id: str) -> Tuple[sqlite3.Row, sqlite3.Row]:
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        raise HTTPException(404, "Session not found")

    admin_user = _get_admin_user_conn(conn)
    if not admin_user:
        raise HTTPException(500, "Admin account is unavailable.")

    return sess, admin_user


def _insert_admin_owned_session_copy_conn(
    conn: sqlite3.Connection,
    sess: sqlite3.Row,
    admin_user_id: str,
    *,
    title_suffix: str,
    locked: int,
    current_round: int,
    round_open: int,
    action_open: int,
) -> str:
    new_session_id = _generate_unique_token_conn(conn, "sessions", "id", nbytes=6)
    join_token = _generate_unique_token_conn(
        conn,
        "sessions",
        "join_token",
        nbytes=6,
        reserved={new_session_id, str(sess["id"])},
    )
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, group_size, multiplier, endowment, rounds, created_at,
            locked, current_round, round_open, action_open, join_token,
            owner_user_id, teacher_removed_at, teacher_removed_by_user_id
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            new_session_id,
            f"{sess['title']}{title_suffix}",
            int(sess["group_size"]),
            float(sess["multiplier"]),
            int(sess["endowment"]),
            int(sess["rounds"]),
            created_at,
            int(locked),
            int(current_round),
            int(round_open),
            int(action_open),
            join_token,
            admin_user_id,
            None,
            None,
        ),
    )
    return new_session_id


def _copy_whitelist_conn(conn: sqlite3.Connection, source_session_id: str, target_session_id: str) -> None:
    whitelist_rows = conn.execute(
        """
        SELECT student_id, name, added_at
        FROM whitelist
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (source_session_id,),
    ).fetchall()
    if whitelist_rows:
        conn.executemany(
            """
            INSERT INTO whitelist(session_id, student_id, name, added_at)
            VALUES(?,?,?,?)
        """,
            [(target_session_id, row["student_id"], row["name"], row["added_at"]) for row in whitelist_rows],
        )


def _duplicate_session_conn(conn: sqlite3.Connection, session_id: str) -> str:
    sess, admin_user = _get_source_session_and_admin_conn(conn, session_id)
    new_session_id = _insert_admin_owned_session_copy_conn(
        conn,
        sess,
        str(admin_user["id"]),
        title_suffix=" (Copy)",
        locked=int(sess["locked"]),
        current_round=int(sess["current_round"]),
        round_open=int(sess["round_open"]),
        action_open=int(sess["action_open"]),
    )

    _copy_whitelist_conn(conn, session_id, new_session_id)

    students = conn.execute(
        """
        SELECT id, student_id, name, anonymous_id, joined_at, group_no, group_pos
        FROM students
        WHERE session_id=?
        ORDER BY joined_at ASC, id ASC
    """,
        (session_id,),
    ).fetchall()
    student_id_map: Dict[str, str] = {}
    reserved_student_ids: set[str] = set()
    if students:
        student_rows = []
        for student in students:
            new_student_id = _generate_unique_token_conn(
                conn,
                "students",
                "id",
                nbytes=8,
                reserved=reserved_student_ids,
            )
            reserved_student_ids.add(new_student_id)
            student_id_map[str(student["id"])] = new_student_id
            student_rows.append(
                (
                    new_student_id,
                    new_session_id,
                    student["student_id"],
                    student["name"],
                    student["anonymous_id"],
                    student["joined_at"],
                    student["group_no"],
                    student["group_pos"],
                )
            )
        conn.executemany(
            """
            INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            student_rows,
        )

    def remap_student_id(source_student_id: str) -> str:
        mapped = student_id_map.get(str(source_student_id))
        if not mapped:
            raise RuntimeError(f"Missing duplicated student mapping for session {session_id}")
        return mapped

    contribution_rows = conn.execute(
        """
        SELECT round_no, student_id, contrib, created_at
        FROM contributions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if contribution_rows:
        conn.executemany(
            """
            INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
            VALUES(?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["student_id"])),
                    int(row["contrib"]),
                    row["created_at"],
                )
                for row in contribution_rows
            ],
        )

    action_rows = conn.execute(
        """
        SELECT round_no, actor_student_id, target_student_id, points, created_at
        FROM actions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if action_rows:
        conn.executemany(
            """
            INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
            VALUES(?,?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["actor_student_id"])),
                    remap_student_id(str(row["target_student_id"])),
                    int(row["points"]),
                    row["created_at"],
                )
                for row in action_rows
            ],
        )

    result_rows = conn.execute(
        """
        SELECT round_no, student_id, group_no, group_n, group_total, public_return,
               contrib, phase, phase_round, action_sent, action_received,
               action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
        FROM results
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    if result_rows:
        conn.executemany(
            """
            INSERT INTO results(
                session_id, round_no, student_id, group_no, group_n, group_total,
                public_return, contrib, phase, phase_round, action_sent, action_received,
                action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            [
                (
                    new_session_id,
                    int(row["round_no"]),
                    remap_student_id(str(row["student_id"])),
                    int(row["group_no"]),
                    int(row["group_n"]),
                    int(row["group_total"]),
                    float(row["public_return"]),
                    int(row["contrib"]),
                    row["phase"],
                    int(row["phase_round"]),
                    int(row["action_sent"]),
                    int(row["action_received"]),
                    float(row["action_cost"]),
                    float(row["action_effect"]),
                    float(row["income"]),
                    float(row["cumulative"]),
                    float(row["phase_cumulative"]),
                    row["computed_at"],
                )
                for row in result_rows
            ],
        )

    return new_session_id


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


def _duplicate_session_setup_conn(conn: sqlite3.Connection, session_id: str) -> str:
    sess, admin_user = _get_source_session_and_admin_conn(conn, session_id)
    new_session_id = _insert_admin_owned_session_copy_conn(
        conn,
        sess,
        str(admin_user["id"]),
        title_suffix=" (Setup Copy)",
        locked=0,
        current_round=1,
        round_open=0,
        action_open=0,
    )
    _copy_whitelist_conn(conn, session_id, new_session_id)
    return new_session_id


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


def verify_user_password(user: sqlite3.Row, entered: str) -> Tuple[bool, bool]:
    scheme = str(user["password_scheme"] or "")
    if scheme == PASSWORD_SCHEME_PBKDF2:
        salt = str(user["password_salt"] or "")
        expected = _hash_password_with_salt(entered, salt)
        return hmac.compare_digest(str(user["password_hash"] or ""), expected), False
    if scheme == PASSWORD_SCHEME_LEGACY_ADMIN and user["role"] == USER_ROLE_ADMIN:
        expected = _legacy_hash_password(entered)
        return hmac.compare_digest(str(user["password_hash"] or ""), expected), True
    return False, False


def _is_auth_configured() -> bool:
    return bool(SECRET_KEY) and get_user_by_username("admin") is not None


def _auth_configuration_error() -> str:
    if not SECRET_KEY:
        return "Set SECRET_KEY to sign login cookies."
    return "No admin account is available. On first boot set ADMIN_PASSWORD so the app can create one."


def _must_configure_auth() -> None:
    if not _is_auth_configured():
        raise HTTPException(500, _auth_configuration_error())


def make_auth_token(user: sqlite3.Row) -> str:
    _must_configure_auth()
    payload = {
        "uid": str(user["id"]),
        "role": str(user["role"]),
        "pv": int(user["password_version"]),
        "ts": int(dt.datetime.now().timestamp() * 1000),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _sign(body.encode("utf-8"))
    return f"{body}.{sig}"


def verify_auth_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        _must_configure_auth()
        if not token or "." not in token:
            return None
        body, sig = token.split(".", 1)
        expected = _sign(body.encode("utf-8"))
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        ts = int(payload.get("ts", 0))
        now_ts = int(dt.datetime.now().timestamp() * 1000)
        if ts <= 0 or now_ts - ts > AUTH_TOKEN_TTL_SECONDS * 1000:
            return None
        if not payload.get("uid") or not payload.get("role"):
            return None
        payload["pv"] = int(payload.get("pv", 0))
        if payload["pv"] <= 0:
            return None
        return payload
    except Exception:
        return None


def generate_temp_password(length: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    if getattr(request.state, "_auth_loaded", False):
        return getattr(request.state, "_auth_user", None)

    request.state._auth_loaded = True
    request.state._auth_user = None

    payload = verify_auth_token(request.cookies.get(AUTH_COOKIE_NAME, ""))
    if not payload:
        return None

    user = get_user_by_id(str(payload["uid"]))
    if not user:
        return None
    if user["disabled_at"] is not None:
        return None
    if str(user["role"]) != str(payload["role"]):
        return None
    if int(user["password_version"]) != int(payload["pv"]):
        return None

    request.state._auth_user = user
    return user


def _auth_gate(request: Request) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse(url="/admin/login", status_code=303)
    return user, None


def _management_gate(request: Request, *, admin_only: bool = False) -> Tuple[Optional[sqlite3.Row], Optional[RedirectResponse]]:
    user, gate = _auth_gate(request)
    if gate:
        return None, gate
    if admin_only and user["role"] != USER_ROLE_ADMIN:
        raise HTTPException(404, "Not found")
    if int(user["must_change_password"]) == 1:
        return user, RedirectResponse(url="/admin?pw_change_required=1", status_code=303)
    return user, None


def get_session(session_id: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute(
        """
        SELECT s.*,
               owner.username AS owner_username,
               removed_by.username AS teacher_removed_by_username
        FROM sessions s
        LEFT JOIN users owner ON owner.id=s.owner_user_id
        LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
        WHERE s.id=?
    """,
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def get_session_by_join_token(join_token: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute(
        """
        SELECT s.*,
               owner.username AS owner_username,
               removed_by.username AS teacher_removed_by_username
        FROM sessions s
        LEFT JOIN users owner ON owner.id=s.owner_user_id
        LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
        WHERE s.join_token=?
    """,
        (join_token,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Join link is invalid or expired.")
    return row


def get_session_for_user(session_id: str, user: sqlite3.Row) -> sqlite3.Row:
    sess = get_session(session_id)
    if user["role"] != USER_ROLE_ADMIN and sess["owner_user_id"] != user["id"]:
        raise HTTPException(404, "Session not found")
    return sess


def list_sessions(owner_user_id: Optional[str] = None) -> List[sqlite3.Row]:
    conn = db()
    try:
        if owner_user_id is None:
            rows = conn.execute(
                """
                SELECT s.*,
                       owner.username AS owner_username,
                       removed_by.username AS teacher_removed_by_username
                FROM sessions s
                LEFT JOIN users owner ON owner.id=s.owner_user_id
                LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
                ORDER BY s.created_at DESC
            """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.*,
                       owner.username AS owner_username,
                       removed_by.username AS teacher_removed_by_username
                FROM sessions s
                LEFT JOIN users owner ON owner.id=s.owner_user_id
                LEFT JOIN users removed_by ON removed_by.id=s.teacher_removed_by_user_id
                WHERE s.owner_user_id=?
                ORDER BY s.created_at DESC
            """,
                (owner_user_id,),
            ).fetchall()
        return rows
    finally:
        conn.close()


# ---------------- Core helpers ----------------


def list_students(session_id: str) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()
    conn.close()
    return rows


def session_counts(session_id: str) -> Dict[str, int]:
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM students WHERE session_id=?", (session_id,)
    ).fetchone()["c"]
    w = conn.execute(
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?", (session_id,)
    ).fetchone()["c"]
    conn.close()
    return {"students": int(n), "whitelist": int(w)}


def ensure_int(value: str, min_v: int, max_v: int, field: str) -> int:
    try:
        v = int(value)
    except Exception:
        raise HTTPException(400, f"{field} must be an integer")
    if v < min_v or v > max_v:
        raise HTTPException(400, f"{field} must be between {min_v} and {max_v}")
    return v


def next_round_after_compute(current_round: int, rounds: int) -> int:
    if current_round >= rounds:
        return rounds
    return current_round + 1


def count_computed_rounds(session_id: str) -> int:
    conn = db()
    c = conn.execute(
        "SELECT COUNT(DISTINCT round_no) AS c FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    conn.close()
    return int(c or 0)


def phase_computed_counts_conn(conn: sqlite3.Connection, session_id: str) -> Dict[str, int]:
    counts = {phase: 0 for phase in PHASES}
    rows = conn.execute(
        """
        SELECT round_no
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    for row in rows:
        phase, _ = phase_for_round(int(row["round_no"]))
        counts[phase] += 1
    return counts


def build_phase_status(total_rounds: int, computed_counts: Dict[str, int]) -> List[Dict[str, object]]:
    statuses = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0:
            continue
        statuses.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "total_rounds": phase_rounds,
                "computed_rounds": int(computed_counts.get(phase, 0)),
                "completed": int(computed_counts.get(phase, 0)) >= phase_rounds,
            }
        )
    return statuses


def build_student_phase_report_conn(
    conn: sqlite3.Connection,
    session_id: str,
    student_row_id: str,
    phase: str,
) -> Optional[Dict[str, object]]:
    rows = conn.execute(
        """
        SELECT round_no, phase_round, group_no, group_n, group_total, contrib,
               action_sent, action_received, action_cost, action_effect,
               income, phase_cumulative, computed_at
        FROM results
        WHERE session_id=? AND student_id=? AND phase=?
        ORDER BY round_no ASC
    """,
        (session_id, student_row_id, phase),
    ).fetchall()
    if not rows:
        return None

    group_no = int(rows[0]["group_no"])
    group_n = int(rows[0]["group_n"])
    group_round_rows = conn.execute(
        """
        SELECT round_no,
               MIN(phase_round) AS phase_round,
               MAX(group_total) AS group_total,
               SUM(income) AS group_income,
               AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=? AND phase=? AND group_no=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id, phase, group_no),
    ).fetchall()

    student_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "contrib": int(row["contrib"]),
            "action_sent": int(row["action_sent"]),
            "action_received": int(row["action_received"]),
            "action_cost": float(row["action_cost"]),
            "action_effect": float(row["action_effect"]),
            "income": float(row["income"]),
            "phase_cumulative": float(row["phase_cumulative"]),
            "computed_at": row["computed_at"],
        }
        for row in rows
    ]
    group_rows = [
        {
            "round": int(row["round_no"]),
            "phase_round": int(row["phase_round"]),
            "group_total": int(row["group_total"]),
            "group_income": float(row["group_income"]),
            "avg_contrib": float(row["avg_contrib"]),
        }
        for row in group_round_rows
    ]

    total_group_contrib = sum(row["group_total"] for row in group_rows)
    total_group_income = sum(row["group_income"] for row in group_rows)

    return {
        "phase": phase,
        "phase_label": phase_label(phase),
        "group_no": group_no,
        "group_n": group_n,
        "student_summary": {
            "total_contrib": sum(row["contrib"] for row in student_rows),
            "total_income": sum(row["income"] for row in student_rows),
            "action_sent": sum(row["action_sent"] for row in student_rows),
            "action_received": sum(row["action_received"] for row in student_rows),
            "final_phase_cumulative": student_rows[-1]["phase_cumulative"],
        },
        "student_rows": student_rows,
        "group_summary": {
            "total_contrib": total_group_contrib,
            "total_income": total_group_income,
            "avg_contrib": (total_group_contrib / (group_n * len(group_rows))) if group_rows and group_n > 0 else 0.0,
        },
        "group_rows": group_rows,
    }


def build_teacher_phase_reports_conn(
    conn: sqlite3.Connection,
    session_id: str,
    total_rounds: int,
    computed_counts: Dict[str, int],
) -> List[Dict[str, object]]:
    reports = []
    for phase in PHASES:
        phase_rounds = phase_round_count_for_session(total_rounds, phase)
        if phase_rounds <= 0 or int(computed_counts.get(phase, 0)) < phase_rounds:
            continue

        group_rows = conn.execute(
            """
            SELECT group_no,
                   MAX(group_n) AS group_n,
                   SUM(group_total) AS total_contrib,
                   SUM(group_income) AS total_income,
                   AVG(avg_contrib) AS avg_contrib
            FROM (
                SELECT group_no,
                       round_no,
                       MAX(group_n) AS group_n,
                       MAX(group_total) AS group_total,
                       SUM(income) AS group_income,
                       AVG(contrib) AS avg_contrib
                FROM results
                WHERE session_id=? AND phase=?
                GROUP BY group_no, round_no
            ) per_round
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, phase),
        ).fetchall()

        student_rows = conn.execute(
            """
            SELECT s.anonymous_id, s.student_id, s.name, r.group_no,
                   SUM(r.contrib) AS total_contrib,
                   SUM(r.income) AS total_income,
                   SUM(r.action_sent) AS action_sent,
                   SUM(r.action_received) AS action_received,
                   MAX(r.phase_cumulative) AS final_phase_cumulative
            FROM results r
            JOIN students s ON s.id=r.student_id
            WHERE r.session_id=? AND r.phase=?
            GROUP BY r.student_id, s.anonymous_id, s.student_id, s.name, r.group_no, s.group_pos, s.joined_at
            ORDER BY r.group_no ASC, s.group_pos ASC, s.joined_at ASC
        """,
            (session_id, phase),
        ).fetchall()

        groups = [
            {
                "group_no": int(row["group_no"]),
                "group_n": int(row["group_n"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "avg_contrib": float(row["avg_contrib"]),
            }
            for row in group_rows
        ]
        students = [
            {
                "anonymous_id": row["anonymous_id"],
                "student_id": row["student_id"],
                "name": row["name"],
                "group_no": int(row["group_no"]),
                "total_contrib": int(row["total_contrib"]),
                "total_income": float(row["total_income"]),
                "action_sent": int(row["action_sent"]),
                "action_received": int(row["action_received"]),
                "final_phase_cumulative": float(row["final_phase_cumulative"]),
            }
            for row in student_rows
        ]

        reports.append(
            {
                "phase": phase,
                "phase_label": phase_label(phase),
                "groups": groups,
                "students": students,
                "totals": {
                    "total_contrib": sum(row["total_contrib"] for row in groups),
                    "total_income": sum(row["total_income"] for row in groups),
                },
            }
        )
    return reports


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
               COUNT(DISTINCT a.actor_student_id) AS action_submitted
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


# ---------------- Whitelist ----------------

def whitelist_template_csv() -> bytes:
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["student_id", "name"])
    w.writerow(["20260001", "Alice"])
    w.writerow(["20260002", "Bob"])
    return output.getvalue().encode("utf-8-sig")


def parse_whitelist_csv(file_bytes: bytes) -> List[Tuple[str, str]]:
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows: List[Tuple[str, str]] = []
    for i, row in enumerate(reader):
        if not row:
            continue
        if (
            i == 0
            and len(row) >= 2
            and row[0].strip().lower() == "student_id"
            and row[1].strip().lower() == "name"
        ):
            continue
        if len(row) < 2:
            continue
        student_id = row[0].strip()
        name = row[1].strip()
        if not student_id or not name:
            continue
        rows.append((student_id, name))
    return rows


def upsert_whitelist(session_id: str, entries: List[Tuple[str, str]]) -> Dict[str, int]:
    conn = db()
    cur = conn.cursor()
    added_at = now_iso()
    upserted = 0
    for student_id, name in entries:
        cur.execute(
            """
            INSERT INTO whitelist(session_id, student_id, name, added_at)
            VALUES(?,?,?,?)
            ON CONFLICT(session_id, student_id)
            DO UPDATE SET name=excluded.name, added_at=excluded.added_at
        """,
            (session_id, student_id, name, added_at),
        )
        upserted += 1
    conn.commit()
    conn.close()
    return {"upserted": upserted}


def clear_whitelist(session_id: str) -> None:
    conn = db()
    conn.execute("DELETE FROM whitelist WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def delete_session(session_id: str) -> None:
    conn = db()
    try:
        # Start an explicit transaction to ensure all deletes are atomic.
        conn.execute("BEGIN")
        conn.execute("DELETE FROM actions WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM contributions WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM results WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM students WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM whitelist WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def whitelist_check_or_raise(session_id: str, student_id: str, name: str) -> None:
    conn = db()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?", (session_id,)
    ).fetchone()["c"]
    if int(total) == 0:
        conn.close()
        raise HTTPException(403, "Whitelist not imported for this session. Please contact the instructor.")

    row = conn.execute(
        """
        SELECT name FROM whitelist
        WHERE session_id=? AND student_id=?
    """,
        (session_id, student_id),
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(403, "Student ID is not in the whitelist.")
    if str(row["name"]) != name:
        raise HTTPException(403, "Name does not match the whitelist exactly.")


# ---------------- Grouping ----------------

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

    for g in range(min_groups, max_groups + 1):
        base = total // g
        rem = total % g
        sizes = [base + 1 if i < rem else base for i in range(g)]
        if min(sizes) < MIN_GROUP_SIZE or max(sizes) > MAX_GROUP_SIZE:
            continue

        score = (abs((total / g) - preferred), abs(g - target_groups))
        if best_score is None or score < best_score:
            best_score = score
            best_sizes = sizes

    if best_sizes is not None:
        return best_sizes

    # Fallback: near preferred size, relax the lower-bound for very small remainder edge cases.
    g = max(1, round(total / preferred))
    base = total // g
    rem = total % g
    return [base + 1 if i < rem else base for i in range(g)]


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
    ids = [r["id"] for r in students]

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


# ---------------- Rounds / compute ----------------

def open_round(session_id: str, round_no: int):
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=1, action_open=0 WHERE id=?",
        (round_no, session_id),
    )
    conn.commit()
    conn.close()


def close_round(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=0 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def open_action_stage(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0, action_open=1 WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def advance_round(session_id: str, current_round: int, rounds: int) -> None:
    nxt = next_round_after_compute(current_round, rounds)
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=0, action_open=0 WHERE id=?",
        (nxt, session_id),
    )
    conn.commit()
    conn.close()


def compute_results(session_id: str, round_no: int):
    conn = db()
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        conn.close()
        raise HTTPException(404, "Session not found")

    multiplier = float(sess["multiplier"])
    endowment = int(sess["endowment"])
    phase, phase_round = phase_for_round(round_no)

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    students = conn.execute(
        """
        SELECT id, group_no
        FROM students
        WHERE session_id=? AND group_no IS NOT NULL
        ORDER BY group_no ASC, group_pos ASC
    """,
        (session_id,),
    ).fetchall()

    contrib_rows = conn.execute(
        """
        SELECT student_id, contrib FROM contributions
        WHERE session_id=? AND round_no=?
    """,
        (session_id, round_no),
    ).fetchall()
    contrib = {r["student_id"]: int(r["contrib"]) for r in contrib_rows}

    group_totals: Dict[int, int] = {}
    group_ns: Dict[int, int] = {}
    student_group: Dict[str, int] = {}
    for s in students:
        g = int(s["group_no"])
        sid = s["id"]
        student_group[sid] = g
        group_ns[g] = group_ns.get(g, 0) + 1
        group_totals[g] = group_totals.get(g, 0) + contrib.get(sid, 0)

    prev = conn.execute(
        """
        SELECT student_id, cumulative FROM results
        WHERE session_id=? AND round_no=?
    """,
        (session_id, round_no - 1),
    ).fetchall()
    prev_cum = {r["student_id"]: float(r["cumulative"]) for r in prev}
    prev_phase_cum: Dict[str, float] = {}
    if phase_round > 1:
        prev_phase_rows = conn.execute(
            """
            SELECT student_id, phase_cumulative
            FROM results
            WHERE session_id=? AND round_no=?
        """,
            (session_id, round_no - 1),
        ).fetchall()
        prev_phase_cum = {r["student_id"]: float(r["phase_cumulative"]) for r in prev_phase_rows}

    action_sent: Dict[str, int] = {}
    action_received: Dict[str, int] = {}
    if phase in ("reward", "punishment"):
        action_rows = conn.execute(
            """
            SELECT actor_student_id, target_student_id, points
            FROM actions
            WHERE session_id=? AND round_no=?
        """,
            (session_id, round_no),
        ).fetchall()
        for row in action_rows:
            actor = row["actor_student_id"]
            target = row["target_student_id"]
            points = int(row["points"])
            if points <= 0 or actor == target:
                continue
            if actor not in student_group or target not in student_group:
                continue
            if student_group[actor] != student_group[target]:
                continue
            action_sent[actor] = action_sent.get(actor, 0) + points
            action_received[target] = action_received.get(target, 0) + points

    computed_at = now_iso()
    out_rows = []
    for s in students:
        sid = s["id"]
        g = int(s["group_no"])
        gt = int(group_totals[g])
        gn = int(group_ns[g])
        pr = multiplier * gt / gn if gn > 0 else 0.0
        c_i = contrib.get(sid, 0)

        base_income = endowment - c_i + pr
        sent = action_sent.get(sid, 0)
        received = action_received.get(sid, 0)

        if phase == "reward":
            action_cost = ACTION_COST * sent
            action_effect = REWARD_EFFECT * received
        elif phase == "punishment":
            action_cost = ACTION_COST * sent
            action_effect = -PUNISH_EFFECT * received
        else:
            action_cost = 0.0
            action_effect = 0.0

        income = base_income - action_cost + action_effect
        cumulative = prev_cum.get(sid, 0.0) + income
        phase_cumulative = prev_phase_cum.get(sid, 0.0) + income

        out_rows.append(
            (
                session_id,
                round_no,
                sid,
                g,
                gn,
                gt,
                pr,
                c_i,
                phase,
                phase_round,
                sent,
                received,
                action_cost,
                action_effect,
                income,
                cumulative,
                phase_cumulative,
                computed_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, phase, phase_round, action_sent, action_received,
            action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id, round_no, student_id)
        DO UPDATE SET
            group_no=excluded.group_no,
            group_n=excluded.group_n,
            group_total=excluded.group_total,
            public_return=excluded.public_return,
            contrib=excluded.contrib,
            phase=excluded.phase,
            phase_round=excluded.phase_round,
            action_sent=excluded.action_sent,
            action_received=excluded.action_received,
            action_cost=excluded.action_cost,
            action_effect=excluded.action_effect,
            income=excluded.income,
            cumulative=excluded.cumulative,
            phase_cumulative=excluded.phase_cumulative,
            computed_at=excluded.computed_at
    """,
        out_rows,
    )

    conn.commit()
    conn.close()


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


# ---------------- Admin login/logout ----------------

def _render_login_page(request: Request, *, status_code: int = 200, **extra: object):
    context = {
        "request": request,
        "configured": _is_auth_configured(),
        "configuration_error": _auth_configuration_error(),
        "pw_changed": request.query_params.get("pw_changed") == "1",
    }
    context.update(extra)
    return templates.TemplateResponse("admin_login.html", context, status_code=status_code)


def _build_admin_home_context(request: Request, user: sqlite3.Row, **extra: object) -> Dict[str, object]:
    is_admin = user["role"] == USER_ROLE_ADMIN
    must_change_password = int(user["must_change_password"]) == 1
    context: Dict[str, object] = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "must_change_password": must_change_password,
        "pw_change_required": must_change_password or request.query_params.get("pw_change_required") == "1",
        "sessions": list_sessions() if is_admin else list_sessions(str(user["id"])),
        "teachers": list_teachers() if is_admin else [],
    }
    context.update(extra)
    return context


def _render_admin_home(
    request: Request,
    user: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    return templates.TemplateResponse(
        "admin_home.html",
        _build_admin_home_context(request, user, **extra),
        status_code=status_code,
    )


def _get_teacher_or_404(user_id: str) -> sqlite3.Row:
    user = get_user_by_id(user_id)
    if not user or user["role"] != USER_ROLE_TEACHER:
        raise HTTPException(404, "Teacher not found")
    return user


def _share_link_context(sess: sqlite3.Row) -> Dict[str, str]:
    join_url = f"{PUBLIC_BASE_URL}/join/{sess['join_token']}"
    return {
        "join_url": join_url,
        "join_qr_data_uri": qr_svg_data_uri(join_url),
    }


def _render_session_panel(
    request: Request,
    user: sqlite3.Row,
    sess: sqlite3.Row,
    *,
    status_code: int = 200,
    **extra: object,
):
    students = list_students(str(sess["id"]))
    counts = session_counts(str(sess["id"]))
    conn = db()
    total_rounds = int(sess["rounds"])
    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)
    stage = stage_of_session(sess)
    computed_counts = phase_computed_counts_conn(conn, str(sess["id"]))
    phase_statuses = build_phase_status(total_rounds, computed_counts)
    round_progress = current_round_progress_conn(conn, str(sess["id"]), round_no)
    current_round_contrib_rows = (
        current_round_contrib_rows_conn(conn, str(sess["id"]), round_no)
        if stage == "action" and phase in ("reward", "punishment")
        else []
    )
    phase_reports = build_teacher_phase_reports_conn(conn, str(sess["id"]), total_rounds, computed_counts)
    conn.close()

    share_ctx = _share_link_context(sess)
    export_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/export"
    template_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/whitelist/template"
    display_url = f"{PUBLIC_BASE_URL}/display/{sess['id']}"
    share_url = f"{PUBLIC_BASE_URL}/admin/{sess['id']}/share"

    context: Dict[str, object] = {
        "request": request,
        "user": user,
        "is_admin": user["role"] == USER_ROLE_ADMIN,
        "sess": sess,
        "students": students,
        "counts": counts,
        "join_url": share_ctx["join_url"],
        "join_qr_data_uri": share_ctx["join_qr_data_uri"],
        "share_url": share_url,
        "export_url": export_url,
        "template_url": template_url,
        "display_url": display_url,
        "round_ctx": round_context(sess),
        "computed_rounds": count_computed_rounds(str(sess["id"])),
        "phase_statuses": phase_statuses,
        "round_progress": round_progress,
        "current_round_contrib_rows": current_round_contrib_rows,
        "phase_reports": phase_reports,
        "transfer_teachers": list_teachers(active_only=True) if user["role"] == USER_ROLE_ADMIN else [],
    }
    context.update(extra)
    return templates.TemplateResponse("session_panel.html", context, status_code=status_code)


def _render_share_link_page(request: Request, sess: sqlite3.Row):
    context: Dict[str, object] = {
        "request": request,
        "sess": sess,
        "share_api_url": f"{PUBLIC_BASE_URL}/api/admin/{sess['id']}/share_link",
    }
    context.update(_share_link_context(sess))
    return templates.TemplateResponse("share_link.html", context)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _render_login_page(request)


@app.post("/admin/login")
def admin_login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    _must_configure_auth()
    user = get_user_by_username(username)
    if not user:
        return _render_login_page(request, status_code=401, error="Incorrect username or password.")
    if user["disabled_at"] is not None:
        return _render_login_page(request, status_code=403, error="This account is disabled.")

    password_ok, needs_rehash = verify_user_password(user, password)
    if not password_ok:
        return _render_login_page(request, status_code=401, error="Incorrect username or password.")

    if needs_rehash:
        conn = db()
        try:
            _rehash_legacy_user_password_conn(conn, str(user["id"]), password)
            conn.commit()
        finally:
            conn.close()
        user = get_user_by_id(str(user["id"]))

    token = make_auth_token(user)
    redirect_url = "/admin?pw_change_required=1" if int(user["must_change_password"]) == 1 else "/admin"
    resp = RedirectResponse(url=redirect_url, status_code=303)
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=ADMIN_COOKIE_SECURE,
        samesite="lax",
        max_age=AUTH_TOKEN_TTL_SECONDS,
        path="/",
    )
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@app.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


@app.post("/admin/change_password")
def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user, gate = _auth_gate(request)
    if gate:
        return gate

    def _render_error(error: str):
        return _render_admin_home(request, user, status_code=400, pw_error=error)

    password_ok, _ = verify_user_password(user, current_password)
    if not password_ok:
        return _render_error("Current password is incorrect.")
    if not new_password:
        return _render_error("New password must not be empty.")
    if new_password != confirm_password:
        return _render_error("New passwords do not match.")
    set_user_password(str(user["id"]), new_password, must_change_password=False)
    resp = RedirectResponse(url="/admin/login?pw_changed=1", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    resp.delete_cookie(LEGACY_ADMIN_COOKIE_NAME, path="/")
    return resp


# ---------------- Admin pages ----------------

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user, gate = _auth_gate(request)
    if gate:
        return gate
    return _render_admin_home(request, user)


@app.post("/admin/teachers")
def admin_create_teacher(request: Request, username: str = Form(...)):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    try:
        username = _validate_username(username)
        temp_password = generate_temp_password()
        create_user(
            username,
            USER_ROLE_TEACHER,
            temp_password,
            must_change_password=True,
        )
    except HTTPException as exc:
        return _render_admin_home(request, user, status_code=exc.status_code, teacher_error=exc.detail)
    except sqlite3.IntegrityError:
        return _render_admin_home(request, user, status_code=400, teacher_error="That username already exists.")

    return _render_admin_home(
        request,
        user,
        teacher_success=f"Created teacher '{username}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=username,
    )


@app.post("/admin/teachers/{user_id}/disable")
def admin_disable_teacher(request: Request, user_id: str):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), True)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/teachers/{user_id}/enable")
def admin_enable_teacher(request: Request, user_id: str):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    set_user_disabled(str(teacher["id"]), False)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/teachers/{user_id}/reset_password")
def admin_reset_teacher_password(request: Request, user_id: str):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    teacher = _get_teacher_or_404(user_id)
    temp_password = generate_temp_password()
    set_user_password(str(teacher["id"]), temp_password, must_change_password=True)
    return _render_admin_home(
        request,
        user,
        teacher_success=f"Reset password for '{teacher['username']}'.",
        teacher_temp_password=temp_password,
        teacher_temp_password_username=str(teacher["username"]),
    )


@app.post("/admin/create")
def admin_create_session(
    request: Request,
    title: str = Form(...),
    group_size: int = Form(DEFAULT_GROUP_SIZE),
    multiplier: float = Form(1.5),
    endowment: int = Form(10),
    rounds: int = Form(TOTAL_EXPERIMENT_ROUNDS),
):
    user, gate = _management_gate(request)
    if gate:
        return gate

    title = _validate_session_title(title)
    if group_size < MIN_GROUP_SIZE or group_size > MAX_GROUP_SIZE:
        raise HTTPException(400, f"group_size must be {MIN_GROUP_SIZE}..{MAX_GROUP_SIZE}")
    if multiplier <= 0 or multiplier > 10:
        raise HTTPException(400, "multiplier must be >0 and <=10")
    if endowment < 1 or endowment > 100:
        raise HTTPException(400, "endowment must be 1..100")
    if rounds < 1 or rounds > 50:
        raise HTTPException(400, "rounds must be 1..50")

    session_id = secrets.token_urlsafe(6)
    conn = db()
    join_token = _generate_unique_token_conn(conn, "sessions", "join_token", nbytes=6, reserved={session_id})
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, group_size, multiplier, endowment, rounds, created_at,
            locked, current_round, round_open, action_open, join_token, owner_user_id
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            session_id,
            title,
            group_size,
            multiplier,
            endowment,
            rounds,
            now_iso(),
            0,
            1,
            0,
            0,
            join_token,
            str(user["id"]),
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.get("/admin/{session_id}", response_class=HTMLResponse)
def admin_panel(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    return _render_session_panel(request, user, sess)


@app.get("/admin/{session_id}/share", response_class=HTMLResponse)
def admin_share_link_page(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    return _render_share_link_page(request, sess)


@app.post("/admin/{session_id}/title")
def admin_update_session_title(request: Request, session_id: str, title: str = Form(...)):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    try:
        title = _validate_session_title(title)
    except HTTPException as exc:
        return _render_session_panel(request, user, sess, status_code=exc.status_code, title_error=exc.detail)

    set_session_title(session_id, title)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/rotate_join_link")
def admin_rotate_join_link(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    rotate_session_join_token(session_id)
    updated_sess = get_session(session_id)
    return _render_session_panel(
        request,
        user,
        updated_sess,
        join_link_success="Student join link refreshed. The previous join link no longer accepts new joins.",
    )


@app.post("/admin/{session_id}/transfer")
def admin_transfer_session(request: Request, session_id: str, teacher_user_id: str = Form(...)):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    teacher_user_id = teacher_user_id.strip()
    teacher = get_user_by_id(teacher_user_id)
    if not teacher or teacher["role"] != USER_ROLE_TEACHER:
        return _render_session_panel(request, user, sess, status_code=400, transfer_error="Select a valid teacher.")
    if teacher["disabled_at"] is not None:
        return _render_session_panel(
            request,
            user,
            sess,
            status_code=400,
            transfer_error="You cannot transfer a session to a disabled teacher.",
        )
    if sess["owner_user_id"] == teacher["id"]:
        return _render_session_panel(
            request,
            user,
            sess,
            status_code=400,
            transfer_error=f"Session is already assigned to '{teacher['username']}'.",
        )

    transfer_session_owner(session_id, str(teacher["id"]))
    updated_sess = get_session(session_id)
    return _render_session_panel(
        request,
        user,
        updated_sess,
        transfer_success=f"Session transferred to '{teacher['username']}'.",
    )


@app.post("/admin/{session_id}/duplicate")
def admin_duplicate_session(request: Request, session_id: str):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    duplicate_session_id = duplicate_session_as_admin(session_id)
    return RedirectResponse(url=f"/admin/{duplicate_session_id}", status_code=303)


@app.post("/admin/{session_id}/duplicate_setup")
def admin_duplicate_session_setup(request: Request, session_id: str):
    user, gate = _management_gate(request, admin_only=True)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    duplicate_session_id = duplicate_session_setup_as_admin(session_id)
    return RedirectResponse(url=f"/admin/{duplicate_session_id}", status_code=303)


@app.post("/admin/{session_id}/lock")
def admin_lock(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    if int(sess["locked"]) == 0:
        lock_groups(session_id, int(sess["group_size"]))
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/switch_phase")
def admin_switch_phase(request: Request, session_id: str, phase: str = Form(...)):
    user, gate = _management_gate(request)
    if gate:
        return gate

    phase = phase.strip().lower()
    if phase not in PHASES:
        raise HTTPException(400, "invalid phase")

    sess = get_session_for_user(session_id, user)
    rounds = int(sess["rounds"])
    start = phase_start_round(phase)
    if start > rounds:
        raise HTTPException(400, f"This session has only {rounds} rounds; phase {phase} is unavailable.")
    end = min(rounds, start + PHASE_ROUNDS - 1)

    conn = db()
    row = conn.execute(
        "SELECT MAX(round_no) AS r FROM results WHERE session_id=? AND round_no BETWEEN ? AND ?",
        (session_id, start, end),
    ).fetchone()
    max_done = row["r"]

    if max_done is None:
        target_round = start
    else:
        target_round = min(end, int(max_done) + 1)

    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=0, action_open=0 WHERE id=?",
        (target_round, session_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/open_round")
def admin_open_round(request: Request, session_id: str, round_no: Optional[int] = Form(None)):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Please lock groups before opening rounds.")
    if stage_of_session(sess) != "closed":
        raise HTTPException(400, "Current round is already open.")

    if round_no is None:
        round_no = int(sess["current_round"])

    round_no = int(round_no)
    if round_no < 1 or round_no > int(sess["rounds"]):
        raise HTTPException(400, "invalid round")

    open_round(session_id, round_no)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/open_action_stage")
def admin_open_action_stage(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Please lock groups before opening rounds.")

    stage = stage_of_session(sess)
    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)

    if phase not in ("reward", "punishment"):
        raise HTTPException(400, "Baseline rounds do not have an action stage.")
    if stage == "closed":
        raise HTTPException(400, "Open the contribution stage first.")
    if stage == "action":
        raise HTTPException(400, f"{phase_label(phase)} stage is already open.")

    open_action_stage(session_id)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/close_and_compute")
def admin_close_and_compute(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    stage = stage_of_session(sess)
    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    phase, _ = phase_for_round(round_no)

    if stage == "closed":
        raise HTTPException(400, "Round is already closed. Open it first.")

    if phase in ("reward", "punishment") and stage != "action":
        raise HTTPException(400, f"Open the {phase_label(phase)} stage before computing this round.")

    # baseline contribution close, or reward/punishment action close
    close_round(session_id)
    compute_results(session_id, round_no)
    advance_round(session_id, round_no, rounds)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.get("/admin/{session_id}/export")
def admin_export(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    conn = db()
    students = conn.execute(
        """
        SELECT id, anonymous_id, student_id, name, group_no
        FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()

    rounds = int(sess["rounds"])

    contrib_rows = conn.execute(
        "SELECT round_no, student_id, contrib FROM contributions WHERE session_id=?",
        (session_id,),
    ).fetchall()
    contrib_map = {(int(r["round_no"]), r["student_id"]): int(r["contrib"]) for r in contrib_rows}

    result_rows = conn.execute(
        """
        SELECT round_no, student_id, phase, phase_round, income, cumulative, phase_cumulative, action_sent, action_received
        FROM results
        WHERE session_id=?
    """,
        (session_id,),
    ).fetchall()
    result_map = {(int(r["round_no"]), r["student_id"]): r for r in result_rows}
    conn.close()

    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(
        [
            "experiment_id",
            "anonymous_id",
            "student_id",
            "name",
            "phase",
            "phase_round",
            "round_no",
            "contribution",
            "income",
            "cumulative",
            "phase_cumulative",
            "action_sent",
            "action_received",
        ]
    )

    for s in students:
        for r in range(1, rounds + 1):
            default_phase, default_phase_round = phase_for_round(r)
            contrib = contrib_map.get((r, s["id"]), "")
            rr = result_map.get((r, s["id"]))
            if rr:
                phase = rr["phase"]
                phase_round = rr["phase_round"]
                income = rr["income"]
                cumulative = rr["cumulative"]
                phase_cumulative = rr["phase_cumulative"]
                action_sent = rr["action_sent"]
                action_received = rr["action_received"]
            else:
                phase = default_phase
                phase_round = default_phase_round
                income = ""
                cumulative = ""
                phase_cumulative = ""
                action_sent = ""
                action_received = ""

            w.writerow(
                [
                    session_id,
                    s["anonymous_id"],
                    s["student_id"],
                    s["name"],
                    phase,
                    phase_round,
                    r,
                    contrib,
                    income,
                    cumulative,
                    phase_cumulative,
                    action_sent,
                    action_received,
                ]
            )

    data = output.getvalue().encode("utf-8-sig")
    filename = f"public_goods_{session_id}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/admin/{session_id}/whitelist/template")
def admin_whitelist_template(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    data = whitelist_template_csv()
    filename = f"whitelist_template_{session_id}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/admin/{session_id}/whitelist/upload")
async def admin_whitelist_upload(request: Request, session_id: str, file: UploadFile = File(...)):
    user, gate = _management_gate(request)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    content = await file.read()
    try:
        entries = parse_whitelist_csv(content)
    except Exception:
        raise HTTPException(400, "Failed to parse CSV. Use UTF-8 CSV with columns: student_id,name")

    if len(entries) == 0:
        raise HTTPException(400, "CSV is empty or invalid. It must include student_id,name columns.")

    upsert_whitelist(session_id, entries)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/whitelist/clear")
def admin_whitelist_clear(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    clear_whitelist(session_id)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/delete")
def admin_delete_session(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    _ = get_session_for_user(session_id, user)
    if user["role"] == USER_ROLE_ADMIN:
        delete_session(session_id)
    else:
        archive_session_to_admin(session_id, str(user["id"]))
    return RedirectResponse(url="/admin", status_code=303)


# ---------------- Student ----------------

@app.get("/join/{join_token}", response_class=HTMLResponse)
def join_page(request: Request, join_token: str):
    sess = get_session_by_join_token(join_token)
    session_id = str(sess["id"])
    counts = session_counts(session_id)
    return templates.TemplateResponse(
        "join.html",
        {"request": request, "sess": sess, "counts": counts, "join_token": join_token},
    )


@app.post("/join/{join_token}")
def join_submit(join_token: str, student_id: str = Form(...), name: str = Form(...)):
    sess = get_session_by_join_token(join_token)
    session_id = str(sess["id"])
    student_id = student_id.strip()
    name = name.strip()
    if not student_id or not name:
        raise HTTPException(400, "student_id and name required")

    whitelist_check_or_raise(session_id, student_id, name)

    conn = db()
    existing = conn.execute(
        "SELECT id, anonymous_id FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()

    if existing:
        sid = existing["id"]
        if existing["anonymous_id"]:
            conn.execute("UPDATE students SET name=? WHERE id=?", (name, sid))
        else:
            used_ids = {
                r["anonymous_id"]
                for r in conn.execute("SELECT anonymous_id FROM students WHERE session_id=?", (session_id,)).fetchall()
                if r["anonymous_id"]
            }
            anonymous_id = _generate_anonymous_id(used_ids)
            conn.execute("UPDATE students SET name=?, anonymous_id=? WHERE id=?", (name, anonymous_id, sid))
    else:
        used_ids = {
            r["anonymous_id"]
            for r in conn.execute("SELECT anonymous_id FROM students WHERE session_id=?", (session_id,)).fetchall()
            if r["anonymous_id"]
        }
        anonymous_id = _generate_anonymous_id(used_ids)
        sid = secrets.token_urlsafe(8)
        conn.execute(
            """
            INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?,?)
        """,
            (sid, session_id, student_id, name, anonymous_id, now_iso(), None, None),
        )
    conn.commit()
    conn.close()

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    return RedirectResponse(url=f"/s/{session_id}/{student_id}", status_code=303)


@app.get("/s/{session_id}/{student_id}", response_class=HTMLResponse)
def student_page(request: Request, session_id: str, student_id: str):
    sess = get_session(session_id)
    conn = db()
    stu = conn.execute(
        "SELECT * FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    conn.close()
    if not stu:
        return RedirectResponse(url=f"/join/{session_id}", status_code=303)
    return templates.TemplateResponse("student.html", {"request": request, "sess": sess, "stu": stu})


@app.post("/api/{session_id}/submit")
def api_submit(session_id: str, student_id: str = Form(...), contrib: str = Form(...)):
    sess = get_session(session_id)
    if int(sess["round_open"]) != 1 or int(sess["action_open"]) == 1:
        raise HTTPException(400, "Contribution stage is not open")

    c = ensure_int(contrib, 0, int(sess["endowment"]), "contrib")

    conn = db()
    stu = conn.execute(
        "SELECT id FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if not stu:
        conn.close()
        raise HTTPException(404, "student not found")

    sid = stu["id"]
    round_no = int(sess["current_round"])
    conn.execute(
        """
        INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(session_id, round_no, student_id)
        DO UPDATE SET contrib=excluded.contrib, created_at=excluded.created_at
    """,
        (session_id, round_no, sid, c, now_iso()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "round": round_no, "contrib": c}


@app.post("/api/{session_id}/submit_actions")
async def api_submit_actions(session_id: str, request: Request):
    sess = get_session(session_id)
    if int(sess["action_open"]) != 1:
        raise HTTPException(400, "Action stage is not open")

    round_no = int(sess["current_round"])
    phase, _ = phase_for_round(round_no)
    if phase not in ("reward", "punishment"):
        raise HTTPException(400, "Current round has no reward/punishment stage")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")

    student_id = str(payload.get("student_id", "")).strip()
    allocations = payload.get("allocations")
    if not student_id:
        raise HTTPException(400, "student_id required")
    if not isinstance(allocations, dict):
        raise HTTPException(400, "allocations must be an object")

    conn = db()
    stu = conn.execute(
        "SELECT id, group_no FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if not stu:
        conn.close()
        raise HTTPException(404, "student not found")
    if stu["group_no"] is None:
        conn.close()
        raise HTTPException(400, "group not assigned")

    targets = conn.execute(
        """
        SELECT id, anonymous_id
        FROM students
        WHERE session_id=? AND group_no=? AND id<>?
        ORDER BY group_pos ASC, joined_at ASC
    """,
        (session_id, int(stu["group_no"]), stu["id"]),
    ).fetchall()

    target_by_anon = {t["anonymous_id"]: t["id"] for t in targets}

    rows_to_insert = []
    for anon_id, target_id in target_by_anon.items():
        raw_points = allocations.get(anon_id, 0)
        points = ensure_int(str(raw_points), 0, MAX_ACTION_POINTS, f"points[{anon_id}]")
        if points > 0:
            rows_to_insert.append((session_id, round_no, stu["id"], target_id, points, now_iso()))

    conn.execute(
        "DELETE FROM actions WHERE session_id=? AND round_no=? AND actor_student_id=?",
        (session_id, round_no, stu["id"]),
    )

    if rows_to_insert:
        conn.executemany(
            """
            INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
            VALUES(?,?,?,?,?,?)
        """,
            rows_to_insert,
        )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "round": round_no,
        "phase": phase,
        "targets_submitted": len(rows_to_insert),
    }


@app.get("/api/{session_id}/status")
def api_status(session_id: str, student_id: str):
    sess = get_session(session_id)
    conn = db()
    stu = conn.execute(
        "SELECT * FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if not stu:
        conn.close()
        raise HTTPException(404, "student not found")

    cur_r = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_r)
    stage = stage_of_session(sess)
    total_rounds = int(sess["rounds"])
    computed_counts = phase_computed_counts_conn(conn, session_id)
    phase_statuses = build_phase_status(total_rounds, computed_counts)
    current_phase_report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), phase)

    cur_c = conn.execute(
        """
        SELECT contrib FROM contributions
        WHERE session_id=? AND round_no=? AND student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchone()

    submitted_actions_rows = conn.execute(
        """
        SELECT target_student_id, points
        FROM actions
        WHERE session_id=? AND round_no=? AND actor_student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchall()
    submitted_actions = {r["target_student_id"]: int(r["points"]) for r in submitted_actions_rows}

    group_view_rows = []
    action_targets = []
    group_view_visible = stage == "action" and phase in ("reward", "punishment") and stu["group_no"] is not None
    if group_view_visible:
        group_view_rows = conn.execute(
            """
            SELECT s.id, s.anonymous_id, COALESCE(c.contrib, 0) AS contrib
            FROM students s
            LEFT JOIN contributions c
                ON c.session_id=s.session_id
               AND c.round_no=?
               AND c.student_id=s.id
            WHERE s.session_id=? AND s.group_no=?
            ORDER BY s.group_pos ASC, s.joined_at ASC
        """,
            (cur_r, session_id, int(stu["group_no"])),
        ).fetchall()

        for r in group_view_rows:
            if r["id"] == stu["id"]:
                continue
            action_targets.append(
                {
                    "anonymous_id": r["anonymous_id"],
                    "points": submitted_actions.get(r["id"], 0),
                }
            )

    completed_phases = []
    for phase_status in phase_statuses:
        if not phase_status["completed"]:
            continue
        report = build_student_phase_report_conn(conn, session_id, str(stu["id"]), str(phase_status["phase"]))
        if report is None:
            continue
        completed_phases.append(report)

    conn.close()

    current_phase_summary = {
        "phase": phase,
        "phase_label": phase_label(phase),
        "phase_round": phase_round,
        "computed_rounds": int(computed_counts.get(phase, 0)),
        "total_rounds": phase_round_count_for_session(total_rounds, phase),
        "student_phase_cumulative": 0.0,
        "student_total_contrib": 0,
        "group_no": int(stu["group_no"]) if stu["group_no"] is not None else None,
        "group_phase_cumulative": 0.0,
        "group_total_contrib": 0,
    }
    if current_phase_report is not None:
        current_phase_summary.update(
            {
                "student_phase_cumulative": float(
                    current_phase_report["student_summary"]["final_phase_cumulative"]
                ),
                "student_total_contrib": int(current_phase_report["student_summary"]["total_contrib"]),
                "group_no": int(current_phase_report["group_no"]),
                "group_phase_cumulative": float(current_phase_report["group_summary"]["total_income"]),
                "group_total_contrib": int(current_phase_report["group_summary"]["total_contrib"]),
            }
        )

    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
                "group_size": int(sess["group_size"]),
                "multiplier": float(sess["multiplier"]),
                "endowment": int(sess["endowment"]),
                "rounds": int(sess["rounds"]),
                "locked": bool(int(sess["locked"])),
                "current_round": cur_r,
                "round_open": bool(int(sess["round_open"])),
                "action_open": bool(int(sess["action_open"])),
                "phase": phase,
                "phase_label": phase_label(phase),
                "phase_round": phase_round,
                "stage": stage,
            },
            "student": {
                "student_id": stu["student_id"],
                "name": stu["name"],
                "anonymous_id": stu["anonymous_id"],
                "group_no": stu["group_no"],
            },
            "phase_statuses": phase_statuses,
            "current_phase": current_phase_summary,
            "current_round": {
                "round": cur_r,
                "submitted_contrib": int(cur_c["contrib"]) if cur_c else None,
                "group_view_visible": group_view_visible,
            },
            "group_view": [
                {"anonymous_id": r["anonymous_id"], "contrib": int(r["contrib"])}
                for r in group_view_rows
                if r["id"] != stu["id"]
            ],
            "action_targets": action_targets,
            "completed_phases": completed_phases,
        }
    )


# ---------------- Classroom display ----------------

@app.get("/display/{session_id}", response_class=HTMLResponse)
def display_page(request: Request, session_id: str):
    sess = get_session(session_id)
    return templates.TemplateResponse("display.html", {"request": request, "sess": sess})


@app.get("/api/{session_id}/display_status")
def api_display_status(session_id: str):
    sess = get_session(session_id)
    cur_round = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_round)
    stage = stage_of_session(sess)

    conn = db()
    latest_row = conn.execute(
        "SELECT MAX(round_no) AS r FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()
    latest_round = latest_row["r"]

    latest_groups = []
    if latest_round is not None:
        rows = conn.execute(
            """
            SELECT group_no, COUNT(*) AS group_n, SUM(contrib) AS group_total, AVG(contrib) AS avg_contrib
            FROM results
            WHERE session_id=? AND round_no=?
            GROUP BY group_no
            ORDER BY group_no ASC
        """,
            (session_id, int(latest_round)),
        ).fetchall()
        latest_groups = [
            {
                "group_no": int(r["group_no"]),
                "group_n": int(r["group_n"]),
                "group_total": int(r["group_total"]),
                "avg_contrib": float(r["avg_contrib"]),
            }
            for r in rows
        ]

    series_rows = conn.execute(
        """
        SELECT round_no, AVG(contrib) AS avg_contrib
        FROM results
        WHERE session_id=?
        GROUP BY round_no
        ORDER BY round_no ASC
    """,
        (session_id,),
    ).fetchall()
    series = [{"round": int(r["round_no"]), "avg_contrib": float(r["avg_contrib"])} for r in series_rows]

    overall = conn.execute(
        "SELECT AVG(contrib) AS v FROM results WHERE session_id=?",
        (session_id,),
    ).fetchone()["v"]
    conn.close()

    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
                "rounds": int(sess["rounds"]),
                "current_round": cur_round,
                "phase": phase,
                "phase_label": phase_label(phase),
                "phase_round": phase_round,
                "stage": stage,
            },
            "computed_rounds": len(series),
            "latest_computed_round": int(latest_round) if latest_round is not None else None,
            "latest_groups": latest_groups,
            "avg_series": series,
            "overall_avg_contrib": float(overall) if overall is not None else None,
        }
    )


@app.get("/api/admin/{session_id}/share_link")
def api_share_link_status(request: Request, session_id: str):
    user, gate = _management_gate(request)
    if gate:
        return gate

    sess = get_session_for_user(session_id, user)
    payload = _share_link_context(sess)
    return JSONResponse(
        {
            "session": {
                "id": sess["id"],
                "title": sess["title"],
            },
            "join_url": payload["join_url"],
            "join_qr_data_uri": payload["join_qr_data_uri"],
        }
    )
