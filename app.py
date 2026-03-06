from __future__ import annotations

import os
import sqlite3
import secrets
import datetime as dt
import time
import csv
import io
import base64
import json
import hmac
import hashlib
import random
import math
import smtplib
import ssl
from email.mime.text import MIMEText
from typing import List, Dict, Tuple, Optional

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates


APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Azure Web App: /home is persistent
DEFAULT_DB_PATH = "/home/public_goods.db"
DB_PATH = os.environ.get("PUBLIC_GOODS_DB_PATH", DEFAULT_DB_PATH)

# Admin protection
ADMIN_PASSWORD = (os.environ.get("ADMIN_PASSWORD") or "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()  # used to sign admin cookie tokens
ADMIN_COOKIE_NAME = "pg_admin"
ADMIN_TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://public-goods.azurewebsites.net").rstrip("/")

# SMTP configuration (set these in Azure App Settings)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.office365.com").strip()
_smtp_port_raw = os.environ.get("SMTP_PORT", "587")
try:
    SMTP_PORT = int(_smtp_port_raw.strip())
except (TypeError, ValueError):
    SMTP_PORT = 587
if not (1 <= SMTP_PORT <= 65535):
    SMTP_PORT = 587
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()  # defaults to SMTP_USERNAME if not set

# Password reset
# ADMIN_EMAIL: use the env var (stripped) if provided; fall back to SMTP_USERNAME if absent or whitespace.
ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip() or SMTP_USERNAME
RESET_TOKEN_TTL_SECONDS = 10 * 60  # 10 minutes
RESET_COOLDOWN_SECONDS = 60  # minimum gap between successful reset email sends
RESET_ATTEMPT_COOLDOWN_SECONDS = 30  # rate-limit all reset attempts, including failed ones

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

app = FastAPI(title="Public Goods Experiment (Azure + Whitelist + Admin Password)")
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


def init_db():
    conn = db()
    cur = conn.cursor()

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
        action_open INTEGER NOT NULL DEFAULT 0
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
    _ensure_column(conn, "students", "anonymous_id", "TEXT")

    _ensure_column(conn, "results", "contrib", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "phase", "TEXT NOT NULL DEFAULT 'baseline'")
    _ensure_column(conn, "results", "phase_round", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "results", "action_sent", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_received", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_cost", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "results", "action_effect", "REAL NOT NULL DEFAULT 0")

    _backfill_anonymous_ids(conn)

    conn.commit()
    conn.close()


# ---------------- Admin auth (signed cookie token) ----------------

def _must_configure_admin() -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "Server missing ADMIN_PASSWORD configuration.")
    if not SECRET_KEY:
        raise HTTPException(500, "Server missing SECRET_KEY configuration.")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(message: bytes) -> str:
    mac = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    return _b64url(mac)


def make_admin_token() -> str:
    _must_configure_admin()
    payload = {"ts": int(dt.datetime.now().timestamp())}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _sign(body.encode("utf-8"))
    return f"{body}.{sig}"


# In-memory cache for password_changed_at epoch — reduces DB hits on admin requests.
# The cache is refreshed from the DB at most once per _PASSWORD_EPOCH_CACHE_TTL_SECONDS so that
# password changes made by another worker (in multi-process deployments) are picked up within
# that window.  _update_password_epoch() always writes the DB *and* immediately updates the cache
# for the current process, so revocation is instant in single-process deployments.
_PASSWORD_EPOCH_CACHE_TTL_SECONDS = 30  # re-read DB if cache is older than this
_password_epoch_cache: Optional[int] = None
_password_epoch_cache_ts: float = float("-inf")  # time.monotonic() of last DB read; -inf means "never read"


def verify_admin_token(token: str) -> bool:
    global _password_epoch_cache, _password_epoch_cache_ts
    try:
        _must_configure_admin()
        if not token or "." not in token:
            return False
        body, sig = token.split(".", 1)
        expected = _sign(body.encode("utf-8"))
        if not hmac.compare_digest(expected, sig):
            return False
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        ts = int(payload.get("ts", 0))
        now_ts = int(dt.datetime.now().timestamp())
        if ts <= 0 or now_ts - ts > ADMIN_TOKEN_TTL_SECONDS:
            return False
        # Reject tokens issued before the last password change (server-side revocation).
        # Refresh the cache from the DB if the cached value is stale (TTL expired) or absent.
        cache_age = time.monotonic() - _password_epoch_cache_ts
        if _password_epoch_cache is None or cache_age >= _PASSWORD_EPOCH_CACHE_TTL_SECONDS:
            epoch_data = get_setting("password_changed_at")
            # If the setting is missing (e.g. fresh DB), treat as "no password change yet"
            # by using epoch 0. Genuine DB exceptions will still be caught by the outer try.
            if epoch_data is None:
                _password_epoch_cache = 0
                _password_epoch_cache_ts = time.monotonic()
            else:
                epoch_str = str(epoch_data).strip()
                if not epoch_str:
                    # No password change recorded yet; treat as "no revocation epoch"
                    _password_epoch_cache = 0
                    _password_epoch_cache_ts = time.monotonic()
                else:
                    try:
                        _password_epoch_cache = int(epoch_str)
                    except ValueError:
                        # Malformed epoch data — fail closed to avoid keeping old sessions alive
                        return False
                    _password_epoch_cache_ts = time.monotonic()
        if _password_epoch_cache is not None and ts <= _password_epoch_cache:
            return False
        return True
    except Exception:
        return False


def _update_password_epoch() -> None:
    """Record the current timestamp as the password-changed epoch for session revocation."""
    global _password_epoch_cache, _password_epoch_cache_ts
    now = int(dt.datetime.now().timestamp())
    set_setting("password_changed_at", str(now))
    _password_epoch_cache = now
    _password_epoch_cache_ts = time.monotonic()


def _hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with SECRET_KEY as salt."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        SECRET_KEY.encode("utf-8"),
        100_000,
    )
    return _b64url(dk)


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


def check_admin_password(entered: str) -> bool:
    """Check if the entered password matches the current admin password.
    Checks the DB-stored hash first; falls back to the ADMIN_PASSWORD env var."""
    stored_hash = get_setting("admin_password_hash")
    if stored_hash is not None:
        return hmac.compare_digest(stored_hash, _hash_password(entered))
    return entered == ADMIN_PASSWORD


# ---------------- Password reset ----------------

def _hmac_token(token: str) -> str:
    """Return HMAC-SHA256 hex digest of a reset token using SECRET_KEY."""
    return hmac.new(SECRET_KEY.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def claim_reset_attempt() -> int:
    """Atomically check the per-attempt DoS cooldown and claim a new attempt slot.

    Uses BEGIN IMMEDIATE to acquire an exclusive write lock before the read-modify-write,
    preventing concurrent requests from both passing the cooldown window.

    Returns seconds remaining in the cooldown (0 if the attempt slot was successfully
    claimed; >0 if still cooling down — the caller should back off).
    """
    now = int(dt.datetime.now().timestamp())
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'password_reset_last_attempt'"
        ).fetchone()
        if row is not None:
            try:
                last_attempt = int(row["value"].strip())
                remaining = max(0, (last_attempt + RESET_ATTEMPT_COOLDOWN_SECONDS) - now)
            except ValueError:
                remaining = 0
        else:
            remaining = 0

        if remaining > 0:
            conn.rollback()
            return remaining

        # Claim the slot by writing the current timestamp.
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('password_reset_last_attempt', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(now),),
        )
        conn.commit()
        return 0
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _store_reset_token_and_record_send(token: str) -> None:
    """Atomically store the HMAC reset token hash and record the successful send timestamp.

    Uses a single DB connection and commit so that both settings are written together.
    This prevents the race where the process crashes between two separate set_setting() calls,
    leaving the admin with a cooldown timestamp but no stored token (or vice versa).
    """
    now = int(dt.datetime.now().timestamp())
    upsert_sql = (
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn = db()
    try:
        conn.execute(upsert_sql, (
            "password_reset_token",
            json.dumps({
                "token_hash": _hmac_token(token),
                "expires_at": now + RESET_TOKEN_TTL_SECONDS,
            }),
        ))
        conn.execute(upsert_sql, ("password_reset_last_sent", str(now)))
        conn.commit()
    finally:
        conn.close()


def verify_reset_token(token: str) -> bool:
    """Return True if the token exists and has not expired."""
    data = get_setting("password_reset_token")
    if not data:
        return False
    try:
        obj = json.loads(data)
        if not hmac.compare_digest(obj.get("token_hash", ""), _hmac_token(token)):
            return False
        if int(dt.datetime.now().timestamp()) > int(obj.get("expires_at", 0)):
            return False
        return True
    except Exception:
        return False


def get_reset_cooldown_remaining() -> int:
    """Return seconds remaining in the post-send cooldown (set only after a successful send), or 0 if none."""
    data = get_setting("password_reset_last_sent")
    if not data:
        return 0
    try:
        last_sent = int(data.strip())
        cooldown_until = last_sent + RESET_COOLDOWN_SECONDS
        return max(0, cooldown_until - int(dt.datetime.now().timestamp()))
    except ValueError:
        return 0


def get_attempt_cooldown_remaining() -> int:
    """Return seconds remaining in the per-attempt DoS cooldown (set before each SMTP attempt), or 0 if none."""
    data = get_setting("password_reset_last_attempt")
    if not data:
        return 0
    try:
        last_attempt = int(data.strip())
        cooldown_until = last_attempt + RESET_ATTEMPT_COOLDOWN_SECONDS
        return max(0, cooldown_until - int(dt.datetime.now().timestamp()))
    except ValueError:
        return 0


def delete_setting(key: str) -> None:
    """Delete a setting by key."""
    conn = db()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def invalidate_reset_token() -> None:
    """Delete any stored password reset token."""
    delete_setting("password_reset_token")


def send_reset_email(token: str) -> bool:
    """Send a password reset email to the admin. Returns True on success."""
    if not SMTP_SERVER or not SMTP_USERNAME or not SMTP_PASSWORD or not ADMIN_EMAIL:
        return False
    reset_url = f"{PUBLIC_BASE_URL}/admin/reset_password/{token}"
    ttl_minutes = RESET_TOKEN_TTL_SECONDS // 60
    body = (
        "You have requested a password reset for the Public Goods Experiment admin panel.\n\n"
        "Click the link below to reset your password:\n\n"
        f"{reset_url}\n\n"
        f"This link is valid for {ttl_minutes} minutes. If you did not request this, please ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Password Reset - Public Goods Experiment"
    msg["From"] = SMTP_FROM or SMTP_USERNAME
    msg["To"] = ADMIN_EMAIL
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        return False


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    return verify_admin_token(token)


def _admin_gate(request: Request) -> Optional[RedirectResponse]:
    if not is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


# ---------------- Core helpers ----------------

def get_session(session_id: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


def list_sessions() -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


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
                computed_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, phase, phase_round, action_sent, action_received,
            action_cost, action_effect, income, cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
def home():
    return RedirectResponse(url="/admin", status_code=302)


# ---------------- Admin login/logout ----------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    configured = bool(ADMIN_PASSWORD) and bool(SECRET_KEY)
    pw_changed = request.query_params.get("pw_changed") == "1"
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "configured": configured, "pw_changed": pw_changed}
    )


@app.post("/admin/login")
def admin_login_submit(request: Request, password: str = Form(...)):
    _must_configure_admin()
    if not check_admin_password(password):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "configured": True, "error": "Incorrect password"},
            status_code=401,
        )
    token = make_admin_token()
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        secure=ADMIN_COOKIE_SECURE,
        samesite="lax",
        max_age=ADMIN_TOKEN_TTL_SECONDS,
        path="/",
    )
    return resp


@app.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return resp


def _forgot_password_secret_key_error(request: Request):
    """Return a 503 template response when SECRET_KEY is not configured."""
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "secret_key_missing": True,
            "reset_ttl_minutes": RESET_TOKEN_TTL_SECONDS // 60,
        },
        status_code=503,
    )


@app.get("/admin/forgot_password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    if not SECRET_KEY:
        return _forgot_password_secret_key_error(request)
    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "reset_ttl_minutes": RESET_TOKEN_TTL_SECONDS // 60},
    )


@app.post("/admin/forgot_password", response_class=HTMLResponse)
def forgot_password_submit(request: Request):
    smtp_configured = (
        bool(SMTP_SERVER)
        and bool(SMTP_USERNAME)
        and bool(SMTP_PASSWORD)
        and bool(ADMIN_EMAIL)
    )
    reset_ttl_minutes = RESET_TOKEN_TTL_SECONDS // 60

    if not SECRET_KEY:
        return _forgot_password_secret_key_error(request)

    # Short-circuit early when SMTP is not configured: skip cooldown recording and token
    # generation entirely so misconfigured servers never show "Too many requests".
    if not smtp_configured:
        return templates.TemplateResponse(
            "forgot_password.html",
            {
                "request": request,
                "sent": False,
                "smtp_configured": False,
                "reset_ttl_minutes": reset_ttl_minutes,
            },
        )

    # Check send cooldown first: only set after a successful email send.
    send_remaining = get_reset_cooldown_remaining()
    if send_remaining > 0:
        return templates.TemplateResponse(
            "forgot_password.html",
            {
                "request": request,
                "cooldown": send_remaining,
                "smtp_configured": smtp_configured,
                "reset_ttl_minutes": reset_ttl_minutes,
            },
        )

    # Atomic attempt cooldown check-and-set: uses BEGIN IMMEDIATE so concurrent requests
    # cannot both pass the cooldown window before either writes its timestamp.
    attempt_remaining = claim_reset_attempt()
    if attempt_remaining > 0:
        return templates.TemplateResponse(
            "forgot_password.html",
            {
                "request": request,
                "attempt_cooldown": attempt_remaining,
                "smtp_configured": smtp_configured,
                "reset_ttl_minutes": reset_ttl_minutes,
            },
        )

    token = secrets.token_urlsafe(32)
    sent = send_reset_email(token)
    if sent:
        _store_reset_token_and_record_send(token)  # atomic: token hash + send timestamp
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "sent": sent,
            "smtp_configured": smtp_configured,
            "reset_ttl_minutes": reset_ttl_minutes,
        },
    )


def _reset_password_secret_key_error(request: Request, token: str):
    """Return a 503 template response when SECRET_KEY is not configured."""
    return templates.TemplateResponse(
        "reset_password.html",
        {
            "request": request,
            "valid": False,
            "secret_key_missing": True,
            "token": token,
            "reset_ttl_minutes": RESET_TOKEN_TTL_SECONDS // 60,
        },
        status_code=503,
    )


@app.get("/admin/reset_password/{token}", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str):
    if not SECRET_KEY:
        return _reset_password_secret_key_error(request, token)
    valid = verify_reset_token(token)
    return templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "valid": valid, "token": token, "reset_ttl_minutes": RESET_TOKEN_TTL_SECONDS // 60},
    )


@app.post("/admin/reset_password/{token}", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    reset_ttl_minutes = RESET_TOKEN_TTL_SECONDS // 60
    if not SECRET_KEY:
        return _reset_password_secret_key_error(request, token)
    if not verify_reset_token(token):
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "valid": False, "token": token, "reset_ttl_minutes": reset_ttl_minutes},
            status_code=400,
        )
    if not new_password:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "valid": True, "token": token, "error": "Password must not be empty.", "reset_ttl_minutes": reset_ttl_minutes},
            status_code=400,
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "valid": True, "token": token, "error": "Passwords do not match.", "reset_ttl_minutes": reset_ttl_minutes},
            status_code=400,
        )
    set_setting("admin_password_hash", _hash_password(new_password))
    _update_password_epoch()
    invalidate_reset_token()
    resp = RedirectResponse(url="/admin/login?pw_changed=1", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return resp


@app.post("/admin/change_password")
def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    gate = _admin_gate(request)
    if gate:
        return gate
    sessions = list_sessions()

    def _render_error(error: str):
        return templates.TemplateResponse(
            "admin_home.html",
            {"request": request, "sessions": sessions, "pw_error": error},
            status_code=400,
        )

    if not check_admin_password(current_password):
        return _render_error("Current password is incorrect.")
    if not new_password:
        return _render_error("New password must not be empty.")
    if new_password != confirm_password:
        return _render_error("New passwords do not match.")
    set_setting("admin_password_hash", _hash_password(new_password))
    _update_password_epoch()
    resp = RedirectResponse(url="/admin/login?pw_changed=1", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return resp


# ---------------- Admin pages ----------------

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    gate = _admin_gate(request)
    if gate:
        return gate
    sessions = list_sessions()
    return templates.TemplateResponse("admin_home.html", {"request": request, "sessions": sessions})


@app.post("/admin/create")
def admin_create_session(
    request: Request,
    title: str = Form(...),
    group_size: int = Form(DEFAULT_GROUP_SIZE),
    multiplier: float = Form(1.5),
    endowment: int = Form(10),
    rounds: int = Form(TOTAL_EXPERIMENT_ROUNDS),
):
    gate = _admin_gate(request)
    if gate:
        return gate

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
    conn.execute(
        """
        INSERT INTO sessions(id, title, group_size, multiplier, endowment, rounds, created_at, locked, current_round, round_open, action_open)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """,
        (session_id, title, group_size, multiplier, endowment, rounds, now_iso(), 0, 1, 0, 0),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.get("/admin/{session_id}", response_class=HTMLResponse)
def admin_panel(request: Request, session_id: str):
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
    students = list_students(session_id)
    counts = session_counts(session_id)

    join_url = f"{PUBLIC_BASE_URL}/join/{session_id}"
    export_url = f"{PUBLIC_BASE_URL}/admin/{session_id}/export"
    template_url = f"{PUBLIC_BASE_URL}/admin/{session_id}/whitelist/template"
    display_url = f"{PUBLIC_BASE_URL}/display/{session_id}"

    ctx = round_context(sess)
    computed_rounds = count_computed_rounds(session_id)

    return templates.TemplateResponse(
        "admin_panel.html",
        {
            "request": request,
            "sess": sess,
            "students": students,
            "counts": counts,
            "join_url": join_url,
            "export_url": export_url,
            "template_url": template_url,
            "display_url": display_url,
            "round_ctx": ctx,
            "computed_rounds": computed_rounds,
        },
    )


@app.post("/admin/{session_id}/lock")
def admin_lock(request: Request, session_id: str):
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
    if int(sess["locked"]) == 0:
        lock_groups(session_id, int(sess["group_size"]))
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/switch_phase")
def admin_switch_phase(request: Request, session_id: str, phase: str = Form(...)):
    gate = _admin_gate(request)
    if gate:
        return gate

    phase = phase.strip().lower()
    if phase not in PHASES:
        raise HTTPException(400, "invalid phase")

    sess = get_session(session_id)
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
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
    if int(sess["locked"]) != 1:
        raise HTTPException(400, "Please lock groups before opening rounds.")

    if round_no is None:
        round_no = int(sess["current_round"])

    round_no = int(round_no)
    if round_no < 1 or round_no > int(sess["rounds"]):
        raise HTTPException(400, "invalid round")

    open_round(session_id, round_no)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/close_and_compute")
def admin_close_and_compute(request: Request, session_id: str):
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
    stage = stage_of_session(sess)
    round_no = int(sess["current_round"])
    rounds = int(sess["rounds"])
    phase, _ = phase_for_round(round_no)

    if stage == "closed":
        raise HTTPException(400, "Round is already closed. Open it first.")

    if stage == "contribution" and phase in ("reward", "punishment"):
        open_action_stage(session_id)
        return RedirectResponse(url=f"/admin/{session_id}", status_code=303)

    # baseline contribution close, or reward/punishment action close
    close_round(session_id)
    compute_results(session_id, round_no)
    advance_round(session_id, round_no, rounds)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.get("/admin/{session_id}/export")
def admin_export(request: Request, session_id: str):
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
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
        SELECT round_no, student_id, phase, phase_round, income, cumulative, action_sent, action_received
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
                action_sent = rr["action_sent"]
                action_received = rr["action_received"]
            else:
                phase = default_phase
                phase_round = default_phase_round
                income = ""
                cumulative = ""
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
    gate = _admin_gate(request)
    if gate:
        return gate

    _ = get_session(session_id)
    data = whitelist_template_csv()
    filename = f"whitelist_template_{session_id}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/admin/{session_id}/whitelist/upload")
async def admin_whitelist_upload(request: Request, session_id: str, file: UploadFile = File(...)):
    gate = _admin_gate(request)
    if gate:
        return gate

    _ = get_session(session_id)
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
    gate = _admin_gate(request)
    if gate:
        return gate

    _ = get_session(session_id)
    clear_whitelist(session_id)
    return RedirectResponse(url=f"/admin/{session_id}", status_code=303)


@app.post("/admin/{session_id}/delete")
def admin_delete_session(request: Request, session_id: str):
    gate = _admin_gate(request)
    if gate:
        return gate

    _ = get_session(session_id)
    delete_session(session_id)
    return RedirectResponse(url="/admin", status_code=303)


# ---------------- Student ----------------

@app.get("/join/{session_id}", response_class=HTMLResponse)
def join_page(request: Request, session_id: str):
    sess = get_session(session_id)
    counts = session_counts(session_id)
    return templates.TemplateResponse("join.html", {"request": request, "sess": sess, "counts": counts})


@app.post("/join/{session_id}")
def join_submit(session_id: str, student_id: str = Form(...), name: str = Form(...)):
    sess = get_session(session_id)
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
        points = ensure_int(str(raw_points), 0, 10, f"points[{anon_id}]")
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

    rows = conn.execute(
        """
        SELECT round_no, phase, phase_round, group_no, group_n, group_total,
               public_return, contrib, action_sent, action_received, action_cost,
               action_effect, income, cumulative, computed_at
        FROM results
        WHERE session_id=? AND student_id=?
        ORDER BY round_no ASC
    """,
        (session_id, stu["id"]),
    ).fetchall()

    cur_r = int(sess["current_round"])
    phase, phase_round = phase_for_round(cur_r)
    stage = stage_of_session(sess)

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
    if stu["group_no"] is not None:
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

    history = [
        {
            "round": int(r["round_no"]),
            "phase": r["phase"],
            "phase_label": phase_label(r["phase"]),
            "phase_round": int(r["phase_round"]),
            "group_no": int(r["group_no"]),
            "group_n": int(r["group_n"]),
            "group_total": int(r["group_total"]),
            "public_return": float(r["public_return"]),
            "contrib": int(r["contrib"]),
            "action_sent": int(r["action_sent"]),
            "action_received": int(r["action_received"]),
            "action_cost": float(r["action_cost"]),
            "action_effect": float(r["action_effect"]),
            "income": float(r["income"]),
            "cumulative": float(r["cumulative"]),
            "computed_at": r["computed_at"],
        }
        for r in rows
    ]

    latest_feedback = history[-1] if history else None
    latest_group_contrib = []
    if latest_feedback is not None:
        latest_rows = conn.execute(
            """
            SELECT s.anonymous_id, r.contrib
            FROM results r
            JOIN students s ON s.id=r.student_id
            WHERE r.session_id=? AND r.round_no=? AND r.group_no=?
            ORDER BY s.group_pos ASC, s.joined_at ASC
        """,
            (session_id, int(latest_feedback["round"]), int(latest_feedback["group_no"])),
        ).fetchall()
        latest_group_contrib = [
            {"anonymous_id": r["anonymous_id"], "contrib": int(r["contrib"])}
            for r in latest_rows
        ]

    conn.close()

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
            "current_round": {
                "round": cur_r,
                "submitted_contrib": int(cur_c["contrib"]) if cur_c else None,
            },
            "group_view": [
                {"anonymous_id": r["anonymous_id"], "contrib": int(r["contrib"])}
                for r in group_view_rows
            ],
            "action_targets": action_targets,
            "history": history,
            "latest_feedback": latest_feedback,
            "latest_group_contrib": latest_group_contrib,
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
