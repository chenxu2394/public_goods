from __future__ import annotations

import csv
import io
import secrets
import sqlite3
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from .auth import _hash_password_record, _legacy_hash_password, _normalize_username, _validate_username
from .config import (
    ADMIN_PASSWORD,
    PASSWORD_SCHEME_LEGACY_ADMIN,
    PASSWORD_SCHEME_PBKDF2,
    TOTAL_EXPERIMENT_ROUNDS,
    USER_ROLE_ADMIN,
    USER_ROLE_TEACHER,
)
from .db import (
    _generate_anonymous_id,
    _generate_unique_token_conn,
    _get_setting_conn,
    _run_write_with_retry,
    db,
    now_iso,
)


def _validate_session_title(title: str) -> str:
    value = title.strip()
    if not value:
        raise HTTPException(400, "Session title must not be empty.")
    if len(value) > 200:
        raise HTTPException(400, "Session title must be at most 200 characters.")
    return value


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


def _get_teacher_or_404(user_id: str) -> sqlite3.Row:
    user = get_user_by_id(user_id)
    if not user or user["role"] != USER_ROLE_TEACHER:
        raise HTTPException(404, "Teacher not found")
    return user


def create_session_record(
    owner_user_id: str,
    title: str,
    *,
    group_size: int,
    multiplier: float,
    endowment: int,
    rounds: int = TOTAL_EXPERIMENT_ROUNDS,
) -> str:
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
            owner_user_id,
        ),
    )
    conn.commit()
    conn.close()
    return session_id


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


def get_student_by_public_id(session_id: str, student_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute(
            "SELECT * FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()
    finally:
        conn.close()


def session_counts(session_id: str) -> Dict[str, int]:
    conn = db()
    student_total = conn.execute(
        "SELECT COUNT(*) AS c FROM students WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    whitelist_total = conn.execute(
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?",
        (session_id,),
    ).fetchone()["c"]
    conn.close()
    return {"students": int(student_total), "whitelist": int(whitelist_total)}


def whitelist_template_csv() -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_id", "name"])
    writer.writerow(["20260001", "Alice"])
    writer.writerow(["20260002", "Bob"])
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
        "SELECT COUNT(*) AS c FROM whitelist WHERE session_id=?",
        (session_id,),
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


def upsert_joined_student(session_id: str, student_id: str, name: str) -> str:
    def _write_join(conn: sqlite3.Connection) -> str:
        existing = conn.execute(
            "SELECT id, anonymous_id FROM students WHERE session_id=? AND student_id=?",
            (session_id, student_id),
        ).fetchone()

        if existing:
            sid = str(existing["id"])
            if existing["anonymous_id"]:
                conn.execute("UPDATE students SET name=? WHERE id=?", (name, sid))
            else:
                used_ids = {
                    row["anonymous_id"]
                    for row in conn.execute(
                        "SELECT anonymous_id FROM students WHERE session_id=?",
                        (session_id,),
                    ).fetchall()
                    if row["anonymous_id"]
                }
                anonymous_id = _generate_anonymous_id(used_ids)
                conn.execute("UPDATE students SET name=?, anonymous_id=? WHERE id=?", (name, anonymous_id, sid))
            return sid

        used_ids = {
            row["anonymous_id"]
            for row in conn.execute("SELECT anonymous_id FROM students WHERE session_id=?", (session_id,)).fetchall()
            if row["anonymous_id"]
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
        return sid

    return _run_write_with_retry(_write_join)
