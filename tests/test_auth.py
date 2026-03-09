import base64
import hashlib
import importlib
import re
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _legacy_hash(secret_key: str, password: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        secret_key.encode("utf-8"),
        100_000,
    )
    return base64.urlsafe_b64encode(dk).decode("utf-8").rstrip("=")


def _load_app(monkeypatch, tmp_path: Path, *, admin_password: str | None, secret_key: str, seed_db=None):
    db_path = tmp_path / "public_goods.db"
    if seed_db is not None:
        seed_db(db_path, secret_key)

    monkeypatch.setenv("PUBLIC_GOODS_DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", secret_key)
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", "0")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    if admin_password is None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASSWORD", admin_password)

    sys.modules.pop("app", None)
    import app as app_module

    return importlib.reload(app_module), db_path


def _login(client: TestClient, username: str, password: str):
    client.cookies.clear()
    return client.post(
        "/admin/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _extract_temp_password(html: str) -> str:
    match = re.search(r'Temporary password.*?<span class="mono">([^<]+)</span>', html, re.S)
    assert match is not None
    return match.group(1)


def _create_teacher(client: TestClient, username: str) -> str:
    response = client.post("/admin/teachers", data={"username": username})
    assert response.status_code == 200
    return _extract_temp_password(response.text)


def _change_password(client: TestClient, current_password: str, new_password: str):
    return client.post(
        "/admin/change_password",
        data={
            "current_password": current_password,
            "new_password": new_password,
            "confirm_password": new_password,
        },
        follow_redirects=False,
    )


def _create_session(client: TestClient, title: str) -> str:
    response = client.post(
        "/admin/create",
        data={
            "title": title,
            "group_size": "5",
            "multiplier": "1.5",
            "endowment": "10",
            "rounds": "30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def _setup_grouped_session(client: TestClient, app_module, title: str = "Experiment Session") -> tuple[str, list[tuple[str, str]]]:
    session_id = _create_session(client, title)
    students = [("20260001", "Alice"), ("20260002", "Bob"), ("20260003", "Cara")]
    app_module.upsert_whitelist(session_id, students)
    join_token = app_module.get_session(session_id)["join_token"]

    for student_id, name in students:
        response = client.post(
            f"/join/{join_token}",
            data={"student_id": student_id, "name": name},
            follow_redirects=False,
        )
        assert response.status_code == 303

    response = client.post(f"/admin/{session_id}/lock", follow_redirects=False)
    assert response.status_code == 303
    return session_id, students


def _submit_contributions(client: TestClient, session_id: str, contributions: dict[str, int]) -> None:
    for student_id, contrib in contributions.items():
        response = client.post(
            f"/api/{session_id}/submit",
            data={"student_id": student_id, "contrib": str(contrib)},
        )
        assert response.status_code == 200


def _create_demo_class(client: TestClient, session_id: str, student_count: int = 24):
    response = client.post(
        f"/admin/{session_id}/demo/create_class",
        data={"student_count": str(student_count)},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return response


def _seed_legacy_results_without_phase_cumulative(db_path: Path, _secret_key: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            round_no INTEGER NOT NULL,
            student_id TEXT NOT NULL,
            group_no INTEGER NOT NULL,
            group_n INTEGER NOT NULL,
            group_total INTEGER NOT NULL,
            public_return REAL NOT NULL,
            contrib INTEGER NOT NULL DEFAULT 0,
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
    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, action_sent, action_received, action_cost,
            action_effect, income, cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        [
            ("sess-legacy", 1, "stu-1", 1, 3, 6, 3.0, 1, 0, 0, 0.0, 0.0, 12.0, 12.0, "2026-03-08T09:00:00"),
            ("sess-legacy", 11, "stu-1", 1, 3, 6, 3.0, 1, 0, 0, 0.0, 0.0, 9.0, 21.0, "2026-03-08T10:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def _insert_sample_session_snapshot(
    app_module,
    session_id: str,
    *,
    owner_user_id: str,
    title: str = "Snapshot Session",
    teacher_removed_by_user_id: str | None = None,
) -> None:
    teacher_removed_at = "2026-03-08T12:45:00" if teacher_removed_by_user_id else None

    conn = sqlite3.connect(app_module.DB_PATH)
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
            session_id,
            title,
            5,
            1.8,
            12,
            24,
            "2026-03-08T09:00:00",
            1,
            12,
            0,
            1,
            f"join-{session_id}",
            owner_user_id,
            teacher_removed_at,
            teacher_removed_by_user_id,
        ),
    )
    conn.executemany(
        """
        INSERT INTO whitelist(session_id, student_id, name, added_at)
        VALUES(?,?,?,?)
    """,
        [
            (session_id, "20260001", "Alice", "2026-03-08T08:30:00"),
            (session_id, "20260002", "Bob", "2026-03-08T08:31:00"),
            (session_id, "20260003", "Cara", "2026-03-08T08:32:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
        VALUES(?,?,?,?,?,?,?,?)
    """,
        [
            ("stu-alpha", session_id, "20260001", "Alice", "A2", "2026-03-08T09:05:00", 1, 1),
            ("stu-beta", session_id, "20260002", "Bob", "B3", "2026-03-08T09:06:00", 1, 2),
            ("stu-gamma", session_id, "20260003", "Cara", "C4", "2026-03-08T09:07:00", 1, 3),
        ],
    )
    conn.executemany(
        """
        INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
        VALUES(?,?,?,?,?)
    """,
        [
            (session_id, 12, "stu-alpha", 4, "2026-03-08T09:20:00"),
            (session_id, 12, "stu-beta", 5, "2026-03-08T09:21:00"),
            (session_id, 12, "stu-gamma", 6, "2026-03-08T09:22:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
        VALUES(?,?,?,?,?,?)
    """,
        [
            (session_id, 12, "stu-alpha", "stu-beta", 2, "2026-03-08T09:25:00"),
            (session_id, 12, "stu-beta", "stu-gamma", 1, "2026-03-08T09:26:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, phase, phase_round, action_sent, action_received,
            action_cost, action_effect, income, cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        [
            (session_id, 11, "stu-alpha", 1, 3, 12, 7.2, 3, "reward", 1, 1, 0, 1.0, 0.0, 16.2, 88.2, "2026-03-08T09:10:00"),
            (session_id, 11, "stu-beta", 1, 3, 12, 7.2, 4, "reward", 1, 0, 1, 0.0, 2.0, 17.2, 90.2, "2026-03-08T09:10:00"),
            (session_id, 11, "stu-gamma", 1, 3, 12, 7.2, 5, "reward", 1, 0, 0, 0.0, 0.0, 14.2, 84.2, "2026-03-08T09:10:00"),
        ],
    )
    conn.commit()
    conn.close()


def _fetch_session_snapshot(app_module, session_id: str):
    conn = sqlite3.connect(app_module.DB_PATH)
    conn.row_factory = sqlite3.Row

    session_row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    assert session_row is not None

    students = conn.execute(
        """
        SELECT id, student_id, name, anonymous_id, joined_at, group_no, group_pos
        FROM students
        WHERE session_id=?
        ORDER BY joined_at ASC, id ASC
    """,
        (session_id,),
    ).fetchall()
    student_public_ids = {row["id"]: row["student_id"] for row in students}

    contributions = conn.execute(
        """
        SELECT round_no, student_id, contrib, created_at
        FROM contributions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    actions = conn.execute(
        """
        SELECT round_no, actor_student_id, target_student_id, points, created_at
        FROM actions
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()
    results = conn.execute(
        """
        SELECT round_no, student_id, group_no, group_n, group_total, public_return,
               contrib, phase, phase_round, action_sent, action_received,
               action_cost, action_effect, income, cumulative, computed_at
        FROM results
        WHERE session_id=?
        ORDER BY id ASC
    """,
        (session_id,),
    ).fetchall()

    snapshot = {
        "session": {
            "title": session_row["title"],
            "group_size": session_row["group_size"],
            "multiplier": session_row["multiplier"],
            "endowment": session_row["endowment"],
            "rounds": session_row["rounds"],
            "locked": session_row["locked"],
            "current_round": session_row["current_round"],
            "round_open": session_row["round_open"],
            "action_open": session_row["action_open"],
            "created_at": session_row["created_at"],
            "owner_user_id": session_row["owner_user_id"],
            "teacher_removed_at": session_row["teacher_removed_at"],
            "teacher_removed_by_user_id": session_row["teacher_removed_by_user_id"],
        },
        "whitelist": [
            (row["student_id"], row["name"], row["added_at"])
            for row in conn.execute(
                """
                SELECT student_id, name, added_at
                FROM whitelist
                WHERE session_id=?
                ORDER BY id ASC
            """,
                (session_id,),
            ).fetchall()
        ],
        "students": [
            (
                row["student_id"],
                row["name"],
                row["anonymous_id"],
                row["joined_at"],
                row["group_no"],
                row["group_pos"],
            )
            for row in students
        ],
        "contributions": [
            (row["round_no"], student_public_ids[row["student_id"]], row["contrib"], row["created_at"])
            for row in contributions
        ],
        "actions": [
            (
                row["round_no"],
                student_public_ids[row["actor_student_id"]],
                student_public_ids[row["target_student_id"]],
                row["points"],
                row["created_at"],
            )
            for row in actions
        ],
        "results": [
            (
                row["round_no"],
                student_public_ids[row["student_id"]],
                row["group_no"],
                row["group_n"],
                row["group_total"],
                row["public_return"],
                row["contrib"],
                row["phase"],
                row["phase_round"],
                row["action_sent"],
                row["action_received"],
                row["action_cost"],
                row["action_effect"],
                row["income"],
                row["cumulative"],
                row["computed_at"],
            )
            for row in results
        ],
        "internal_student_ids": set(student_public_ids.keys()),
        "contribution_refs": {row["student_id"] for row in contributions},
        "action_refs": {row["actor_student_id"] for row in actions} | {row["target_student_id"] for row in actions},
        "result_refs": {row["student_id"] for row in results},
    }
    conn.close()
    return snapshot


def test_init_db_configures_sqlite_pragmas(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    app_module.init_db()

    conn = sqlite3.connect(app_module.DB_PATH)
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    assert str(journal_mode).lower() == "wal"

    conn = app_module.db()
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert int(busy_timeout) == app_module.SQLITE_BUSY_TIMEOUT_MS


def test_run_write_with_retry_retries_locked_sqlite_errors(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )
    app_module.init_db()
    monkeypatch.setattr(app_module.time, "sleep", lambda _: None)

    attempts = {"count": 0}

    def work(conn):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise sqlite3.OperationalError("database is locked")
        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("retry_key", "retry_value"))
        return "ok"

    result = app_module._run_write_with_retry(work)

    assert result == "ok"
    assert attempts["count"] == 3

    conn = sqlite3.connect(app_module.DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key=?", ("retry_key",)).fetchone()
    conn.close()
    assert row == ("retry_value",)


def _seed_legacy_admin_db(db_path: Path, secret_key: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("admin_password_hash", _legacy_hash(secret_key, "legacy-admin-pass")),
    )
    conn.commit()
    conn.close()


def _seed_legacy_session_db(db_path: Path, _secret_key: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions(
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
    conn.execute(
        """
        INSERT INTO sessions(id, title, group_size, multiplier, endowment, rounds, created_at, locked, current_round, round_open)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """,
        ("sess123", "Seeded Session", 5, 1.5, 10, 30, "2026-03-08T09:00:00", 0, 1, 0),
    )
    conn.commit()
    conn.close()


def test_admin_bootstrap_from_env_password(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        response = _login(client, "admin", "bootstrap-secret")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    admin = app_module.get_user_by_username("admin")
    assert admin is not None
    assert admin["role"] == app_module.USER_ROLE_ADMIN
    assert admin["password_scheme"] == app_module.PASSWORD_SCHEME_PBKDF2


def test_legacy_admin_hash_is_rehashed_on_login(monkeypatch, tmp_path: Path):
    app_module, db_path = _load_app(
        monkeypatch,
        tmp_path,
        admin_password=None,
        secret_key="legacy-secret",
        seed_db=_seed_legacy_admin_db,
    )

    with TestClient(app_module.app) as client:
        admin = app_module.get_user_by_username("admin")
        assert admin is not None
        assert admin["password_scheme"] == app_module.PASSWORD_SCHEME_LEGACY_ADMIN

        response = _login(client, "admin", "legacy-admin-pass")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"

    admin = app_module.get_user_by_username("admin")
    assert admin["password_scheme"] == app_module.PASSWORD_SCHEME_PBKDF2
    assert admin["password_salt"]

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key='admin_password_hash'").fetchone()
    conn.close()
    assert row is None


def test_existing_sessions_are_backfilled_to_admin_owner(monkeypatch, tmp_path: Path):
    app_module, db_path = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
        seed_db=_seed_legacy_session_db,
    )

    with TestClient(app_module.app) as client:
        admin = app_module.get_user_by_username("admin")
        assert admin is not None

        join_page = client.get("/join/sess123")
        assert join_page.status_code == 200
        assert 'action="/join/sess123"' in join_page.text

    conn = sqlite3.connect(db_path)
    owner_user_id, join_token = conn.execute(
        "SELECT owner_user_id, join_token FROM sessions WHERE id='sess123'"
    ).fetchone()
    conn.close()

    assert owner_user_id == admin["id"]
    assert join_token == "sess123"


def test_teacher_must_change_password_and_cannot_access_other_teacher_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")

        response = _login(client, "Teacher1", teacher1_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        blocked = client.post(
            "/admin/create",
            data={
                "title": "Blocked Session",
                "group_size": "5",
                "multiplier": "1.5",
                "endowment": "10",
                "rounds": "30",
            },
            follow_redirects=False,
        )
        assert blocked.status_code == 303
        assert blocked.headers["location"] == "/admin?pw_change_required=1"

        response = _change_password(client, teacher1_temp, "Teacher1-final-pass")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login?pw_changed=1"

        response = _login(client, "Teacher1", "Teacher1-final-pass")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"
        session_id = _create_session(client, "Teacher 1 Session")

        home = client.get("/admin")
        assert home.status_code == 200
        assert "Teacher 1 Session" in home.text

        response = _login(client, "Teacher2", teacher2_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        response = _change_password(client, teacher2_temp, "Teacher2-final-pass")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login?pw_changed=1"

        response = _login(client, "Teacher2", "Teacher2-final-pass")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        home = client.get("/admin")
        assert home.status_code == 200
        assert "Teacher 1 Session" not in home.text

        forbidden = client.get(f"/admin/{session_id}", follow_redirects=False)
        assert forbidden.status_code == 404


def test_admin_can_disable_enable_and_reset_teacher_password(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")
        teacher = app_module.get_user_by_username("Teacher1")
        assert teacher is not None

        response = _login(client, "Teacher1", teacher_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        response = _change_password(client, teacher_temp, "Teacher1-final-pass")
        assert response.status_code == 303

        response = _login(client, "Teacher1", "Teacher1-final-pass")
        assert response.status_code == 303
        teacher_cookie = client.cookies.get(app_module.AUTH_COOKIE_NAME)
        assert teacher_cookie

        assert _login(client, "admin", "bootstrap-secret").status_code == 303

        response = client.post(f"/admin/teachers/{teacher['id']}/disable", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        with TestClient(app_module.app) as stale_client:
            stale_client.cookies.set(app_module.AUTH_COOKIE_NAME, teacher_cookie)
            disabled_response = stale_client.get("/admin", follow_redirects=False)
            assert disabled_response.status_code == 303
            assert disabled_response.headers["location"] == "/admin/login"

        response = client.post(f"/admin/teachers/{teacher['id']}/enable", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        response = client.post(f"/admin/teachers/{teacher['id']}/reset_password")
        assert response.status_code == 200
        reset_temp_password = _extract_temp_password(response.text)

        response = _login(client, "Teacher1", "Teacher1-final-pass")
        assert response.status_code == 401

        response = _login(client, "Teacher1", reset_temp_password)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        with TestClient(app_module.app) as stale_client:
            stale_client.cookies.set(app_module.AUTH_COOKIE_NAME, teacher_cookie)
            reset_response = stale_client.get("/admin", follow_redirects=False)
            assert reset_response.status_code == 303
            assert reset_response.headers["location"] == "/admin/login"


def test_admin_can_transfer_session_to_teacher(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id = _create_session(client, "Legacy Admin Session")

        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")

        teacher1 = app_module.get_user_by_username("Teacher1")
        assert teacher1 is not None

        response = client.post(
            f"/admin/{session_id}/transfer",
            data={"teacher_user_id": teacher1["id"]},
        )
        assert response.status_code == 200
        assert "Session transferred to" in response.text

        session_row = app_module.get_session(session_id)
        assert session_row["owner_user_id"] == teacher1["id"]
        assert session_row["owner_username"] == "Teacher1"

        response = _login(client, "Teacher1", teacher1_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        response = _change_password(client, teacher1_temp, "Teacher1-final-pass")
        assert response.status_code == 303

        response = _login(client, "Teacher1", "Teacher1-final-pass")
        assert response.status_code == 303

        home = client.get("/admin")
        assert home.status_code == 200
        assert "Legacy Admin Session" in home.text

        panel = client.get(f"/admin/{session_id}")
        assert panel.status_code == 200

        response = _login(client, "Teacher2", teacher2_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"

        response = _change_password(client, teacher2_temp, "Teacher2-final-pass")
        assert response.status_code == 303

        response = _login(client, "Teacher2", "Teacher2-final-pass")
        assert response.status_code == 303

        forbidden = client.get(f"/admin/{session_id}", follow_redirects=False)
        assert forbidden.status_code == 404


def test_teacher_delete_archives_session_to_admin(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")
        teacher = app_module.get_user_by_username("Teacher1")
        admin = app_module.get_user_by_username("admin")
        assert teacher is not None
        assert admin is not None

        response = _login(client, "Teacher1", teacher_temp)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?pw_change_required=1"
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        session_id = _create_session(client, "Teacher Archive Session")

        response = client.post(f"/admin/{session_id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        home = client.get("/admin")
        assert "Teacher Archive Session" not in home.text

        forbidden = client.get(f"/admin/{session_id}", follow_redirects=False)
        assert forbidden.status_code == 404

        session_row = app_module.get_session(session_id)
        assert session_row["owner_user_id"] == admin["id"]
        assert session_row["teacher_removed_by_user_id"] == teacher["id"]
        assert session_row["teacher_removed_by_username"] == "Teacher1"
        assert session_row["teacher_removed_at"] is not None

        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        admin_home = client.get("/admin")
        assert admin_home.status_code == 200
        assert "Teacher Archive Session" in admin_home.text
        assert "Removed by Teacher1" in admin_home.text

        admin_panel = client.get(f"/admin/{session_id}")
        assert admin_panel.status_code == 200
        assert "Removed by Teacher1" in admin_panel.text


def test_admin_delete_still_hard_deletes_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id = _create_session(client, "Admin Delete Session")

        response = client.post(f"/admin/{session_id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        missing = client.get(f"/admin/{session_id}", follow_redirects=False)
        assert missing.status_code == 404

        conn = sqlite3.connect(app_module.DB_PATH)
        row = conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        conn.close()
        assert row is None


def test_transfer_clears_teacher_archive_metadata(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")
        teacher1 = app_module.get_user_by_username("Teacher1")
        teacher2 = app_module.get_user_by_username("Teacher2")
        assert teacher1 is not None
        assert teacher2 is not None

        assert _login(client, "Teacher1", teacher1_temp).status_code == 303
        assert _change_password(client, teacher1_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Recoverable Session")
        assert client.post(f"/admin/{session_id}/delete", follow_redirects=False).status_code == 303

        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        response = client.post(
            f"/admin/{session_id}/transfer",
            data={"teacher_user_id": teacher2["id"]},
        )
        assert response.status_code == 200
        assert "Session transferred to" in response.text

        session_row = app_module.get_session(session_id)
        assert session_row["owner_user_id"] == teacher2["id"]
        assert session_row["teacher_removed_at"] is None
        assert session_row["teacher_removed_by_user_id"] is None
        assert session_row["teacher_removed_by_username"] is None

        assert _login(client, "Teacher2", teacher2_temp).status_code == 303
        assert _change_password(client, teacher2_temp, "Teacher2-final-pass").status_code == 303
        assert _login(client, "Teacher2", "Teacher2-final-pass").status_code == 303
        home = client.get("/admin")
        assert home.status_code == 200
        assert "Recoverable Session" in home.text


def test_admin_can_duplicate_full_session_snapshot_as_admin(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        admin = app_module.get_user_by_username("admin")
        assert admin is not None

        source_session_id = "snapshot-source"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=admin["id"],
            title="Snapshot Session",
        )
        source_snapshot = _fetch_session_snapshot(app_module, source_session_id)

        response = client.post(f"/admin/{source_session_id}/duplicate", follow_redirects=False)
        assert response.status_code == 303

        duplicate_session_id = response.headers["location"].rsplit("/", 1)[-1]
        assert duplicate_session_id != source_session_id

        duplicate_snapshot = _fetch_session_snapshot(app_module, duplicate_session_id)

        panel = client.get(response.headers["location"])
        assert panel.status_code == 200
        assert "Snapshot Session (Copy)" in panel.text

        assert duplicate_snapshot["session"]["title"] == "Snapshot Session (Copy)"
        assert duplicate_snapshot["session"]["owner_user_id"] == admin["id"]
        assert duplicate_snapshot["session"]["teacher_removed_at"] is None
        assert duplicate_snapshot["session"]["teacher_removed_by_user_id"] is None
        assert duplicate_snapshot["session"]["created_at"] != source_snapshot["session"]["created_at"]
        assert app_module.get_session(duplicate_session_id)["join_token"] != app_module.get_session(source_session_id)["join_token"]

        for field in (
            "group_size",
            "multiplier",
            "endowment",
            "rounds",
            "locked",
            "current_round",
            "round_open",
            "action_open",
        ):
            assert duplicate_snapshot["session"][field] == source_snapshot["session"][field]

        for section in ("whitelist", "students", "contributions", "actions", "results"):
            assert duplicate_snapshot[section] == source_snapshot[section]

        assert duplicate_snapshot["internal_student_ids"].isdisjoint(source_snapshot["internal_student_ids"])
        assert duplicate_snapshot["contribution_refs"] <= duplicate_snapshot["internal_student_ids"]
        assert duplicate_snapshot["action_refs"] <= duplicate_snapshot["internal_student_ids"]
        assert duplicate_snapshot["result_refs"] <= duplicate_snapshot["internal_student_ids"]
        assert duplicate_snapshot["contribution_refs"].isdisjoint(source_snapshot["internal_student_ids"])
        assert duplicate_snapshot["action_refs"].isdisjoint(source_snapshot["internal_student_ids"])
        assert duplicate_snapshot["result_refs"].isdisjoint(source_snapshot["internal_student_ids"])


def test_admin_duplicate_of_teacher_session_creates_admin_owned_copy(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        _create_teacher(client, "Teacher1")
        admin = app_module.get_user_by_username("admin")
        teacher = app_module.get_user_by_username("Teacher1")
        assert admin is not None
        assert teacher is not None

        source_session_id = "teacher-owned-source"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=teacher["id"],
            title="Teacher Owned Session",
        )

        response = client.post(f"/admin/{source_session_id}/duplicate", follow_redirects=False)
        assert response.status_code == 303

        duplicate_session_id = response.headers["location"].rsplit("/", 1)[-1]
        source_session = app_module.get_session(source_session_id)
        duplicate_session = app_module.get_session(duplicate_session_id)

        assert source_session["owner_user_id"] == teacher["id"]
        assert duplicate_session["owner_user_id"] == admin["id"]
        assert duplicate_session["owner_username"] == "admin"
        assert duplicate_session["title"] == "Teacher Owned Session (Copy)"


def test_admin_duplicate_of_archived_session_clears_archive_metadata(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        _create_teacher(client, "Teacher1")
        admin = app_module.get_user_by_username("admin")
        teacher = app_module.get_user_by_username("Teacher1")
        assert admin is not None
        assert teacher is not None

        source_session_id = "archived-source"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=admin["id"],
            title="Archived Session",
            teacher_removed_by_user_id=teacher["id"],
        )

        response = client.post(f"/admin/{source_session_id}/duplicate", follow_redirects=False)
        assert response.status_code == 303

        duplicate_session_id = response.headers["location"].rsplit("/", 1)[-1]
        duplicate_session = app_module.get_session(duplicate_session_id)

        assert duplicate_session["owner_user_id"] == admin["id"]
        assert duplicate_session["teacher_removed_at"] is None
        assert duplicate_session["teacher_removed_by_user_id"] is None
        assert duplicate_session["teacher_removed_by_username"] is None


def test_teacher_cannot_duplicate_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")
        teacher = app_module.get_user_by_username("Teacher1")
        assert teacher is not None

        source_session_id = "teacher-duplicate-forbidden"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=teacher["id"],
            title="Teacher Session",
        )

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        response = client.post(f"/admin/{source_session_id}/duplicate", follow_redirects=False)
        assert response.status_code == 404


def test_admin_can_duplicate_setup_as_fresh_admin_owned_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        _create_teacher(client, "Teacher1")
        admin = app_module.get_user_by_username("admin")
        teacher = app_module.get_user_by_username("Teacher1")
        assert admin is not None
        assert teacher is not None

        source_session_id = "setup-source"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=teacher["id"],
            title="Reusable Session",
            teacher_removed_by_user_id=teacher["id"],
        )
        source_snapshot = _fetch_session_snapshot(app_module, source_session_id)

        response = client.post(f"/admin/{source_session_id}/duplicate_setup", follow_redirects=False)
        assert response.status_code == 303

        duplicate_session_id = response.headers["location"].rsplit("/", 1)[-1]
        duplicate_snapshot = _fetch_session_snapshot(app_module, duplicate_session_id)

        panel = client.get(response.headers["location"])
        assert panel.status_code == 200
        assert "Reusable Session (Setup Copy)" in panel.text

        assert duplicate_snapshot["session"]["title"] == "Reusable Session (Setup Copy)"
        assert duplicate_snapshot["session"]["owner_user_id"] == admin["id"]
        assert duplicate_snapshot["session"]["teacher_removed_at"] is None
        assert duplicate_snapshot["session"]["teacher_removed_by_user_id"] is None
        assert duplicate_snapshot["session"]["created_at"] != source_snapshot["session"]["created_at"]
        assert app_module.get_session(duplicate_session_id)["join_token"] != app_module.get_session(source_session_id)["join_token"]

        for field in ("group_size", "multiplier", "endowment", "rounds"):
            assert duplicate_snapshot["session"][field] == source_snapshot["session"][field]

        assert duplicate_snapshot["session"]["locked"] == 0
        assert duplicate_snapshot["session"]["current_round"] == 1
        assert duplicate_snapshot["session"]["round_open"] == 0
        assert duplicate_snapshot["session"]["action_open"] == 0

        assert duplicate_snapshot["whitelist"] == source_snapshot["whitelist"]
        assert duplicate_snapshot["students"] == []
        assert duplicate_snapshot["contributions"] == []
        assert duplicate_snapshot["actions"] == []
        assert duplicate_snapshot["results"] == []
        assert duplicate_snapshot["internal_student_ids"] == set()
        assert duplicate_snapshot["contribution_refs"] == set()
        assert duplicate_snapshot["action_refs"] == set()
        assert duplicate_snapshot["result_refs"] == set()


def test_teacher_cannot_duplicate_setup_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")
        teacher = app_module.get_user_by_username("Teacher1")
        assert teacher is not None

        source_session_id = "teacher-setup-forbidden"
        _insert_sample_session_snapshot(
            app_module,
            source_session_id,
            owner_user_id=teacher["id"],
            title="Teacher Setup Session",
        )

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        response = client.post(f"/admin/{source_session_id}/duplicate_setup", follow_redirects=False)
        assert response.status_code == 404


def test_teacher_can_rotate_join_link_and_old_link_expires(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        session_id = _create_session(client, "Rotating Join Link Session")
        app_module.upsert_whitelist(session_id, [("20260001", "Alice")])

        old_join_token = app_module.get_session(session_id)["join_token"]
        assert old_join_token
        assert old_join_token != session_id

        old_join_page = client.get(f"/join/{old_join_token}")
        assert old_join_page.status_code == 200
        assert f'action="/join/{old_join_token}"' in old_join_page.text

        first_join = client.post(
            f"/join/{old_join_token}",
            data={"student_id": "20260001", "name": "Alice"},
            follow_redirects=False,
        )
        assert first_join.status_code == 303
        assert first_join.headers["location"] == f"/s/{session_id}/20260001"

        rotate_response = client.post(f"/admin/{session_id}/rotate_join_link", follow_redirects=False)
        assert rotate_response.status_code == 200
        assert "Student join link refreshed." in rotate_response.text

        new_join_token = app_module.get_session(session_id)["join_token"]
        assert new_join_token != old_join_token
        assert f"/join/{new_join_token}" in rotate_response.text

        stale_get = client.get(f"/join/{old_join_token}", follow_redirects=False)
        assert stale_get.status_code == 404

        stale_post = client.post(
            f"/join/{old_join_token}",
            data={"student_id": "20260001", "name": "Alice"},
            follow_redirects=False,
        )
        assert stale_post.status_code == 404

        fresh_get = client.get(f"/join/{new_join_token}")
        assert fresh_get.status_code == 200
        assert f'action="/join/{new_join_token}"' in fresh_get.text

        fresh_post = client.post(
            f"/join/{new_join_token}",
            data={"student_id": "20260001", "name": "Alice"},
            follow_redirects=False,
        )
        assert fresh_post.status_code == 303
        assert fresh_post.headers["location"] == f"/s/{session_id}/20260001"

        student_page = client.get(f"/s/{session_id}/20260001")
        assert student_page.status_code == 200


def test_admin_can_rotate_teacher_owned_session_join_link(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher Owned Join Link")

        old_join_token = app_module.get_session(session_id)["join_token"]

        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        response = client.post(f"/admin/{session_id}/rotate_join_link", follow_redirects=False)
        assert response.status_code == 200
        assert "Student join link refreshed." in response.text

        new_join_token = app_module.get_session(session_id)["join_token"]
        assert new_join_token != old_join_token


def test_teacher_cannot_rotate_other_users_join_link(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")

        assert _login(client, "Teacher1", teacher1_temp).status_code == 303
        assert _change_password(client, teacher1_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher1 Join Link")
        old_join_token = app_module.get_session(session_id)["join_token"]

        assert _login(client, "Teacher2", teacher2_temp).status_code == 303
        assert _change_password(client, teacher2_temp, "Teacher2-final-pass").status_code == 303
        assert _login(client, "Teacher2", "Teacher2-final-pass").status_code == 303

        response = client.post(f"/admin/{session_id}/rotate_join_link", follow_redirects=False)
        assert response.status_code == 404
        assert app_module.get_session(session_id)["join_token"] == old_join_token


def test_session_panel_includes_share_link_button(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id = _create_session(client, "Share Link Session")

        panel = client.get(f"/admin/{session_id}")
        assert panel.status_code == 200
        assert f'href="http://testserver/admin/{session_id}/share"' in panel.text
        assert 'target="_blank"' in panel.text
        assert "Share Link" in panel.text


def test_teacher_and_admin_can_access_share_link_page_and_api(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")
        teacher = app_module.get_user_by_username("Teacher1")
        assert teacher is not None

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher Share Page")

        teacher_share_page = client.get(f"/admin/{session_id}/share")
        assert teacher_share_page.status_code == 200
        assert "Student join link" in teacher_share_page.text

        teacher_share_api = client.get(f"/api/admin/{session_id}/share_link")
        assert teacher_share_api.status_code == 200
        teacher_payload = teacher_share_api.json()
        assert teacher_payload["session"]["title"] == "Teacher Share Page"
        assert teacher_payload["join_url"].endswith(app_module.get_session(session_id)["join_token"])
        assert teacher_payload["join_qr_data_uri"].startswith("data:image/svg+xml;base64,")

        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        admin_share_page = client.get(f"/admin/{session_id}/share")
        assert admin_share_page.status_code == 200

        admin_share_api = client.get(f"/api/admin/{session_id}/share_link")
        assert admin_share_api.status_code == 200
        assert admin_share_api.json()["join_url"] == teacher_payload["join_url"]


def test_share_link_api_updates_after_join_link_rotation(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Live Share Link")

        before = client.get(f"/api/admin/{session_id}/share_link")
        assert before.status_code == 200
        before_payload = before.json()

        rotate = client.post(f"/admin/{session_id}/rotate_join_link", follow_redirects=False)
        assert rotate.status_code == 200

        after = client.get(f"/api/admin/{session_id}/share_link")
        assert after.status_code == 200
        after_payload = after.json()

        assert after_payload["session"]["title"] == "Live Share Link"
        assert after_payload["join_url"] != before_payload["join_url"]
        assert after_payload["join_qr_data_uri"] != before_payload["join_qr_data_uri"]


def test_teacher_cannot_access_other_users_share_link_page_or_api(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")

        assert _login(client, "Teacher1", teacher1_temp).status_code == 303
        assert _change_password(client, teacher1_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher1 Share Link")

        assert _login(client, "Teacher2", teacher2_temp).status_code == 303
        assert _change_password(client, teacher2_temp, "Teacher2-final-pass").status_code == 303
        assert _login(client, "Teacher2", "Teacher2-final-pass").status_code == 303

        share_page = client.get(f"/admin/{session_id}/share", follow_redirects=False)
        assert share_page.status_code == 404

        share_api = client.get(f"/api/admin/{session_id}/share_link", follow_redirects=False)
        assert share_api.status_code == 404


def test_teacher_can_rename_own_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        session_id = _create_session(client, "Original Teacher Title")
        response = client.post(
            f"/admin/{session_id}/title",
            data={"title": "Renamed Teacher Session"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/{session_id}"

        session_row = app_module.get_session(session_id)
        assert session_row["title"] == "Renamed Teacher Session"

        panel = client.get(f"/admin/{session_id}")
        assert panel.status_code == 200
        assert "Renamed Teacher Session" in panel.text
        assert "Original Teacher Title" not in panel.text


def test_admin_can_rename_teacher_owned_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher Owned Title")

        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        response = client.post(
            f"/admin/{session_id}/title",
            data={"title": "Admin Renamed Title"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/{session_id}"

        session_row = app_module.get_session(session_id)
        assert session_row["title"] == "Admin Renamed Title"

        home = client.get("/admin")
        assert home.status_code == 200
        assert "Admin Renamed Title" in home.text
        assert "Teacher Owned Title" not in home.text


def test_teacher_cannot_rename_other_users_session(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher1_temp = _create_teacher(client, "Teacher1")
        teacher2_temp = _create_teacher(client, "Teacher2")

        assert _login(client, "Teacher1", teacher1_temp).status_code == 303
        assert _change_password(client, teacher1_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303
        session_id = _create_session(client, "Teacher1 Session")

        assert _login(client, "Teacher2", teacher2_temp).status_code == 303
        assert _change_password(client, teacher2_temp, "Teacher2-final-pass").status_code == 303
        assert _login(client, "Teacher2", "Teacher2-final-pass").status_code == 303

        response = client.post(
            f"/admin/{session_id}/title",
            data={"title": "Teacher2 Rename Attempt"},
            follow_redirects=False,
        )
        assert response.status_code == 404

        session_row = app_module.get_session(session_id)
        assert session_row["title"] == "Teacher1 Session"


def test_empty_session_title_is_rejected_on_rename(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id = _create_session(client, "Valid Title")

        response = client.post(
            f"/admin/{session_id}/title",
            data={"title": "   "},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Session title must not be empty." in response.text

        session_row = app_module.get_session(session_id)
        assert session_row["title"] == "Valid Title"


def test_startup_backfills_phase_fields_and_phase_cumulative(monkeypatch, tmp_path: Path):
    app_module, db_path = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
        seed_db=_seed_legacy_results_without_phase_cumulative,
    )

    with TestClient(app_module.app):
        pass

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT round_no, phase, phase_round, phase_cumulative
        FROM results
        ORDER BY round_no ASC
    """
    ).fetchall()
    conn.close()

    assert rows == [
        (1, "baseline", 1, 12.0),
        (11, "reward", 1, 9.0),
    ]


def test_reward_visibility_resets_each_round_and_action_points_are_capped(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id, students = _setup_grouped_session(client, app_module, "Visibility Session")

        response = client.post(
            f"/admin/{session_id}/switch_phase",
            data={"phase": "reward"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = client.post(
            f"/admin/{session_id}/open_round",
            data={"round_no": "11"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        _submit_contributions(client, session_id, {student_id: idx + 1 for idx, (student_id, _) in enumerate(students)})

        contribution_stage = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert contribution_stage.status_code == 200
        contribution_payload = contribution_stage.json()
        assert contribution_payload["session"]["stage"] == "contribution"
        assert contribution_payload["current_round"]["group_view_visible"] is False
        assert contribution_payload["group_view"] == []
        assert contribution_payload["action_targets"] == []

        response = client.post(f"/admin/{session_id}/open_action_stage", follow_redirects=False)
        assert response.status_code == 303

        action_stage = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert action_stage.status_code == 200
        action_payload = action_stage.json()
        assert action_payload["session"]["stage"] == "action"
        assert action_payload["current_round"]["group_view_visible"] is True
        assert sorted(row["contrib"] for row in action_payload["group_view"]) == [2, 3]
        assert len(action_payload["action_targets"]) == 2

        target_anon = action_payload["action_targets"][0]["anonymous_id"]
        response = client.post(
            f"/api/{session_id}/submit_actions",
            json={"student_id": "20260001", "allocations": {target_anon: 6}},
        )
        assert response.status_code == 400
        assert "between 0 and 5" in response.text

        response = client.post(
            f"/api/{session_id}/submit_actions",
            json={"student_id": "20260001", "allocations": {target_anon: 5}},
        )
        assert response.status_code == 200

        response = client.post(f"/admin/{session_id}/close_and_compute", follow_redirects=False)
        assert response.status_code == 303

        response = client.post(
            f"/admin/{session_id}/open_round",
            data={"round_no": "12"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        next_round = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert next_round.status_code == 200
        next_round_payload = next_round.json()
        assert next_round_payload["session"]["stage"] == "contribution"
        assert next_round_payload["session"]["phase"] == "reward"
        assert next_round_payload["session"]["phase_round"] == 2
        assert next_round_payload["current_round"]["group_view_visible"] is False
        assert next_round_payload["group_view"] == []
        assert next_round_payload["action_targets"] == []


def test_phase_cumulative_resets_and_completed_phase_reports_are_retained(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    baseline_contributions = {
        "20260001": 2,
        "20260002": 4,
        "20260003": 6,
    }

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id, _ = _setup_grouped_session(client, app_module, "Phase Summary Session")

        for round_no in range(1, 11):
            response = client.post(
                f"/admin/{session_id}/open_round",
                data={"round_no": str(round_no)},
                follow_redirects=False,
            )
            assert response.status_code == 303
            _submit_contributions(client, session_id, baseline_contributions)
            response = client.post(f"/admin/{session_id}/close_and_compute", follow_redirects=False)
            assert response.status_code == 303

        teacher_panel = client.get(f"/admin/{session_id}")
        assert teacher_panel.status_code == 200
        assert "Baseline Results" in teacher_panel.text

        response = client.post(
            f"/admin/{session_id}/open_round",
            data={"round_no": "11"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        _submit_contributions(
            client,
            session_id,
            {
                "20260001": 1,
                "20260002": 2,
                "20260003": 3,
            },
        )

        response = client.post(f"/admin/{session_id}/open_action_stage", follow_redirects=False)
        assert response.status_code == 303

        alice_status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"}).json()
        alice_target = alice_status["action_targets"][0]["anonymous_id"]

        assert client.post(
            f"/api/{session_id}/submit_actions",
            json={"student_id": "20260001", "allocations": {alice_target: 2}},
        ).status_code == 200

        response = client.post(f"/admin/{session_id}/close_and_compute", follow_redirects=False)
        assert response.status_code == 303

        status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert status.status_code == 200
        payload = status.json()

        assert payload["current_phase"]["phase"] == "reward"
        assert payload["current_phase"]["student_phase_cumulative"] == 10.0
        assert payload["current_phase"]["group_phase_cumulative"] == 35.0
        assert [phase["phase"] for phase in payload["completed_phases"]] == ["baseline"]
        assert payload["completed_phases"][0]["student_summary"]["final_phase_cumulative"] == 140.0

        teacher_panel = client.get(f"/admin/{session_id}")
        assert teacher_panel.status_code == 200
        assert "Baseline Results" in teacher_panel.text
        assert "Within-Group Individual Summary" in teacher_panel.text

        export_response = client.get(f"/admin/{session_id}/export")
        assert export_response.status_code == 200
        export_text = export_response.content.decode("utf-8-sig")
        assert "phase_cumulative" in export_text.splitlines()[0]


def test_teacher_can_seed_demo_class_and_run_demo_round(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        teacher_temp = _create_teacher(client, "Teacher1")

        assert _login(client, "Teacher1", teacher_temp).status_code == 303
        assert _change_password(client, teacher_temp, "Teacher1-final-pass").status_code == 303
        assert _login(client, "Teacher1", "Teacher1-final-pass").status_code == 303

        session_id = _create_session(client, "Demo Session")
        response = _create_demo_class(client, session_id, student_count=12)
        assert "Created demo class with 12 mock students" in response.text
        assert "Demo mode active" in response.text

        session_row = app_module.get_session(session_id)
        assert int(session_row["demo_mode"]) == 1
        assert int(session_row["locked"]) == 1

        counts = app_module.session_counts(session_id)
        assert counts["students"] == 12
        assert counts["whitelist"] == 12

        response = client.post(f"/admin/{session_id}/demo/run_current_round", follow_redirects=False)
        assert response.status_code == 200
        assert "Demo ran round 1" in response.text

        updated_session = app_module.get_session(session_id)
        assert int(updated_session["current_round"]) == 2
        assert int(updated_session["round_open"]) == 0
        assert int(updated_session["action_open"]) == 0

        conn = sqlite3.connect(app_module.DB_PATH)
        results_count = conn.execute("SELECT COUNT(*) FROM results WHERE session_id=?", (session_id,)).fetchone()[0]
        contributions_count = conn.execute(
            "SELECT COUNT(*) FROM contributions WHERE session_id=? AND round_no=1",
            (session_id,),
        ).fetchone()[0]
        conn.close()

        assert results_count == 12
        assert contributions_count == 12


def test_demo_autoplay_current_phase_advances_reward_phase(monkeypatch, tmp_path: Path):
    app_module, _ = _load_app(
        monkeypatch,
        tmp_path,
        admin_password="bootstrap-secret",
        secret_key="secret-for-tests",
    )

    with TestClient(app_module.app) as client:
        assert _login(client, "admin", "bootstrap-secret").status_code == 303
        session_id = _create_session(client, "Demo Autoplay Session")
        _create_demo_class(client, session_id, student_count=15)

        response = client.post(
            f"/admin/{session_id}/switch_phase",
            data={"phase": "reward"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = client.post(f"/admin/{session_id}/demo/run_current_phase", follow_redirects=False)
        assert response.status_code == 200
        assert "Demo autoplay completed Reward" in response.text

        session_row = app_module.get_session(session_id)
        assert int(session_row["current_round"]) == 21

        conn = sqlite3.connect(app_module.DB_PATH)
        reward_rounds = conn.execute(
            "SELECT COUNT(DISTINCT round_no) FROM results WHERE session_id=? AND phase='reward'",
            (session_id,),
        ).fetchone()[0]
        reward_actions = conn.execute(
            "SELECT COUNT(*) FROM actions WHERE session_id=? AND round_no BETWEEN 11 AND 20",
            (session_id,),
        ).fetchone()[0]
        conn.close()

        assert reward_rounds == 10
        assert reward_actions > 0
