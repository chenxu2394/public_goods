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

    with TestClient(app_module.app):
        admin = app_module.get_user_by_username("admin")
        assert admin is not None

    conn = sqlite3.connect(db_path)
    owner_user_id = conn.execute(
        "SELECT owner_user_id FROM sessions WHERE id='sess123'"
    ).fetchone()[0]
    conn.close()

    assert owner_user_id == admin["id"]


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
