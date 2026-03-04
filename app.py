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
from typing import List, Dict, Tuple, Optional

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates


APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Azure Web App: /home is persistent
DEFAULT_DB_PATH = "/home/public_goods.db"
DB_PATH = os.environ.get("PUBLIC_GOODS_DB_PATH", DEFAULT_DB_PATH)

# Admin protection
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")  # used to sign admin cookie tokens
ADMIN_COOKIE_NAME = "pg_admin"
ADMIN_TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://public-goods.azurewebsites.net").rstrip("/")

app = FastAPI(title="Public Goods Experiment (Azure + Whitelist + Admin Password)")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _migrate_student_identifier_columns(conn: sqlite3.Connection) -> None:
    # Backward compatibility for existing DBs that still use student_code.
    for table in ("students", "whitelist"):
        cols = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "student_code" in cols and "student_id" not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN student_code TO student_id")


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
        round_open INTEGER NOT NULL DEFAULT 0
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
    CREATE TABLE IF NOT EXISTS results(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        round_no INTEGER NOT NULL,
        student_id TEXT NOT NULL,
        group_no INTEGER NOT NULL,
        group_n INTEGER NOT NULL,
        group_total INTEGER NOT NULL,
        public_return REAL NOT NULL,
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


def verify_admin_token(token: str) -> bool:
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
        return True
    except Exception:
        return False


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
        ORDER BY joined_at ASC
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

    import random
    random.shuffle(ids)

    updates = []
    for idx, sid in enumerate(ids):
        group_no = idx // group_size + 1
        group_pos = idx % group_size + 1
        updates.append((group_no, group_pos, sid))

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

    group_size = int(sess["group_size"])
    last_group = conn.execute(
        """
        SELECT MAX(group_no) AS g FROM students
        WHERE session_id=? AND group_no IS NOT NULL
    """,
        (session_id,),
    ).fetchone()["g"]
    if last_group is None:
        last_group = 1

    last_count = conn.execute(
        """
        SELECT COUNT(*) AS c FROM students
        WHERE session_id=? AND group_no=?
    """,
        (session_id, last_group),
    ).fetchone()["c"]
    last_count = int(last_count)

    if last_count >= group_size:
        last_group = int(last_group) + 1
        pos = 1
    else:
        pos = last_count + 1

    updates = []
    for r in late:
        updates.append((last_group, pos, r["id"]))
        pos += 1
        if pos > group_size:
            last_group += 1
            pos = 1

    conn.executemany("UPDATE students SET group_no=?, group_pos=? WHERE id=?", updates)
    conn.commit()
    conn.close()


# ---------------- Rounds / compute ----------------

def open_round(session_id: str, round_no: int):
    conn = db()
    conn.execute(
        "UPDATE sessions SET current_round=?, round_open=1 WHERE id=?",
        (round_no, session_id),
    )
    conn.commit()
    conn.close()


def close_round(session_id: str):
    conn = db()
    conn.execute("UPDATE sessions SET round_open=0 WHERE id=?", (session_id,))
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

    if int(sess["locked"]) == 1:
        assign_late_joiner(session_id)

    students = conn.execute(
        """
        SELECT id, group_no FROM students
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
    for s in students:
        g = int(s["group_no"])
        group_ns[g] = group_ns.get(g, 0) + 1
        group_totals[g] = group_totals.get(g, 0) + contrib.get(s["id"], 0)

    prev = conn.execute(
        """
        SELECT student_id, cumulative FROM results
        WHERE session_id=? AND round_no=?
    """,
        (session_id, round_no - 1),
    ).fetchall()
    prev_cum = {r["student_id"]: float(r["cumulative"]) for r in prev}

    computed_at = now_iso()
    out_rows = []
    for s in students:
        sid = s["id"]
        g = int(s["group_no"])
        gt = int(group_totals[g])
        gn = int(group_ns[g])
        pr = multiplier * gt / gn if gn > 0 else 0.0
        c_i = contrib.get(sid, 0)
        income = endowment - c_i + pr
        cumulative = prev_cum.get(sid, 0.0) + income
        out_rows.append((session_id, round_no, sid, g, gn, gt, pr, income, cumulative, computed_at))

    conn.executemany(
        """
        INSERT INTO results(session_id, round_no, student_id, group_no, group_n, group_total, public_return, income, cumulative, computed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id, round_no, student_id)
        DO UPDATE SET
            group_no=excluded.group_no,
            group_n=excluded.group_n,
            group_total=excluded.group_total,
            public_return=excluded.public_return,
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
    group_size: int = Form(5),
    multiplier: float = Form(1.5),
    endowment: int = Form(10),
    rounds: int = Form(10),
):
    gate = _admin_gate(request)
    if gate:
        return gate

    if group_size < 2 or group_size > 20:
        raise HTTPException(400, "group_size must be 2..20")
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
        INSERT INTO sessions(id, title, group_size, multiplier, endowment, rounds, created_at, locked, current_round, round_open)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """,
        (session_id, title, group_size, multiplier, endowment, rounds, now_iso(), 0, 1, 0),
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


@app.post("/admin/{session_id}/open_round")
def admin_open_round(request: Request, session_id: str, round_no: int = Form(...)):
    gate = _admin_gate(request)
    if gate:
        return gate

    sess = get_session(session_id)
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
    round_no = int(sess["current_round"])
    close_round(session_id)
    compute_results(session_id, round_no)
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
        SELECT id, student_id, name, group_no
        FROM students
        WHERE session_id=?
        ORDER BY group_no ASC, group_pos ASC, joined_at ASC
    """,
        (session_id,),
    ).fetchall()

    rounds = int(sess["rounds"])

    contrib = conn.execute(
        "SELECT round_no, student_id, contrib FROM contributions WHERE session_id=?",
        (session_id,),
    ).fetchall()
    contrib_map = {(int(r["round_no"]), r["student_id"]): int(r["contrib"]) for r in contrib}

    res = conn.execute(
        """
        SELECT round_no, student_id, group_n, group_total, public_return, income, cumulative
        FROM results
        WHERE session_id=?
    """,
        (session_id,),
    ).fetchall()
    res_map = {(int(r["round_no"]), r["student_id"]): r for r in res}
    conn.close()

    output = io.StringIO()
    w = csv.writer(output)

    header = ["student_id", "name", "group_no"]
    for r in range(1, rounds + 1):
        header += [f"r{r}_contrib", f"r{r}_group_n", f"r{r}_group_total", f"r{r}_public_return", f"r{r}_income", f"r{r}_cumulative"]
    w.writerow(header)

    for s in students:
        row = [s["student_id"], s["name"], s["group_no"]]
        for r in range(1, rounds + 1):
            c = contrib_map.get((r, s["id"]), "")
            rr = res_map.get((r, s["id"]))
            if rr:
                row += [c, rr["group_n"], rr["group_total"], rr["public_return"], rr["income"], rr["cumulative"]]
            else:
                row += [c, "", "", "", "", ""]
        w.writerow(row)

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
        "SELECT id FROM students WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchone()
    if existing:
        sid = existing["id"]
        conn.execute("UPDATE students SET name=? WHERE id=?", (name, sid))
    else:
        sid = secrets.token_urlsafe(8)
        conn.execute(
            """
            INSERT INTO students(id, session_id, student_id, name, joined_at, group_no, group_pos)
            VALUES(?,?,?,?,?,?,?)
        """,
            (sid, session_id, student_id, name, now_iso(), None, None),
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
    if int(sess["round_open"]) != 1:
        raise HTTPException(400, "Round is not open")

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
        SELECT round_no, group_no, group_n, group_total, public_return, income, cumulative, computed_at
        FROM results
        WHERE session_id=? AND student_id=?
        ORDER BY round_no ASC
    """,
        (session_id, stu["id"]),
    ).fetchall()

    cur_r = int(sess["current_round"])
    cur_c = conn.execute(
        """
        SELECT contrib FROM contributions
        WHERE session_id=? AND round_no=? AND student_id=?
    """,
        (session_id, cur_r, stu["id"]),
    ).fetchone()
    conn.close()

    history = [
        {
            "round": int(r["round_no"]),
            "group_no": int(r["group_no"]),
            "group_n": int(r["group_n"]),
            "group_total": int(r["group_total"]),
            "public_return": float(r["public_return"]),
            "income": float(r["income"]),
            "cumulative": float(r["cumulative"]),
            "computed_at": r["computed_at"],
        }
        for r in rows
    ]

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
            },
            "student": {
                "student_id": stu["student_id"],
                "name": stu["name"],
                "group_no": stu["group_no"],
            },
            "current_round": {
                "round": cur_r,
                "submitted_contrib": int(cur_c["contrib"]) if cur_c else None,
            },
            "history": history,
        }
    )
