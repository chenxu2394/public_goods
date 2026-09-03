import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Sequence

from fastapi.testclient import TestClient


DEFAULT_ADMIN_PASSWORD = "bootstrap-secret"
DEFAULT_SECRET_KEY = "secret-for-tests"


def load_app(monkeypatch, tmp_path: Path, *, admin_password: str | None = DEFAULT_ADMIN_PASSWORD, secret_key: str = DEFAULT_SECRET_KEY, seed_db=None):
    db_path = tmp_path / "public_goods.db"
    if seed_db is not None:
        seed_db(db_path, secret_key)

    monkeypatch.setenv("PUBLIC_GOODS_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
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


def load_initialized_app(monkeypatch, tmp_path: Path, *, admin_password: str | None = DEFAULT_ADMIN_PASSWORD, secret_key: str = DEFAULT_SECRET_KEY, seed_db=None):
    app_module, db_path = load_app(
        monkeypatch,
        tmp_path,
        admin_password=admin_password,
        secret_key=secret_key,
        seed_db=seed_db,
    )
    app_module.init_db()
    return app_module, db_path


def login(client: TestClient, username: str = "admin", password: str = DEFAULT_ADMIN_PASSWORD):
    client.cookies.clear()
    return client.post(
        "/admin/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def create_session(
    client: TestClient,
    title: str,
    *,
    group_size: int = 5,
    multiplier: float = 1.5,
    endowment: int = 10,
    rounds: int = 30,
) -> str:
    response = client.post(
        "/admin/create",
        data={
            "title": title,
            "group_size": str(group_size),
            "multiplier": str(multiplier),
            "endowment": str(endowment),
            "rounds": str(rounds),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def join_students(client: TestClient, join_token: str, students: Iterable[tuple[str, str]]) -> None:
    for student_id, name in students:
        response = client.post(
            f"/join/{join_token}",
            data={"student_id": student_id, "name": name},
            follow_redirects=False,
        )
        assert response.status_code == 303


def setup_grouped_session(
    client: TestClient,
    app_module,
    title: str = "Experiment Session",
    *,
    students: Sequence[tuple[str, str]] | None = None,
    group_size: int = 5,
    multiplier: float = 1.5,
    endowment: int = 10,
    rounds: int = 30,
) -> tuple[str, list[tuple[str, str]], str]:
    student_rows = list(
        students or [("20260001", "Alice"), ("20260002", "Bob"), ("20260003", "Cara")]
    )
    session_id = create_session(
        client,
        title,
        group_size=group_size,
        multiplier=multiplier,
        endowment=endowment,
        rounds=rounds,
    )
    app_module.upsert_whitelist(session_id, student_rows)
    join_token = app_module.get_session(session_id)["join_token"]
    join_students(client, join_token, student_rows)
    response = client.post(f"/admin/{session_id}/lock", follow_redirects=False)
    assert response.status_code == 303
    return session_id, student_rows, join_token


def open_round(client: TestClient, session_id: str, round_no: int, phase: str = "baseline"):
    response = client.post(
        f"/admin/{session_id}/open_round",
        data={"round_no": str(round_no), "phase": phase},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def open_action_stage(client: TestClient, session_id: str):
    response = client.post(f"/admin/{session_id}/open_action_stage", follow_redirects=False)
    assert response.status_code == 303
    return response


def close_and_compute(client: TestClient, session_id: str):
    response = client.post(f"/admin/{session_id}/close_and_compute", follow_redirects=False)
    assert response.status_code == 303
    return response


def submit_contributions(client: TestClient, session_id: str, contributions: dict[str, int]) -> None:
    for student_id, contrib in contributions.items():
        response = client.post(
            f"/api/{session_id}/submit",
            data={"student_id": student_id, "contrib": str(contrib)},
        )
        assert response.status_code == 200


def get_student_rows(app_module, session_id: str) -> dict[str, dict[str, object]]:
    conn = sqlite3.connect(app_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM students
        WHERE session_id=?
        ORDER BY joined_at ASC, id ASC
    """,
        (session_id,),
    ).fetchall()
    conn.close()
    return {str(row["student_id"]): dict(row) for row in rows}


def get_result_rows(app_module, session_id: str, round_no: int) -> dict[str, dict[str, object]]:
    conn = sqlite3.connect(app_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT r.*, s.student_id AS public_student_id
        FROM results r
        JOIN students s ON s.id=r.student_id
        WHERE r.session_id=? AND r.round_no=?
        ORDER BY s.group_no ASC, s.group_pos ASC, s.joined_at ASC
    """,
        (session_id, round_no),
    ).fetchall()
    conn.close()
    return {str(row["public_student_id"]): dict(row) for row in rows}


def insert_session(
    app_module,
    session_id: str,
    *,
    title: str = "Seeded Session",
    group_size: int = 5,
    multiplier: float = 1.5,
    endowment: int = 10,
    rounds: int = 30,
    created_at: str = "2026-03-08T09:00:00",
    locked: int = 0,
    current_round: int = 1,
    current_phase: str | None = None,
    round_open: int = 0,
    action_open: int = 0,
    demo_mode: int = 0,
    join_token: str | None = None,
    owner_user_id: str | None = None,
) -> None:
    if owner_user_id is None:
        admin = app_module.get_user_by_username("admin")
        owner_user_id = str(admin["id"]) if admin is not None else None

    conn = sqlite3.connect(app_module.DB_PATH)
    conn.execute(
        """
        INSERT INTO sessions(
            id, title, group_size, multiplier, endowment, rounds, created_at,
            locked, current_round, current_phase, round_open, action_open, demo_mode, join_token, owner_user_id
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            session_id,
            title,
            group_size,
            multiplier,
            endowment,
            rounds,
            created_at,
            locked,
            current_round,
            current_phase,
            round_open,
            action_open,
            demo_mode,
            join_token or f"join-{session_id}",
            owner_user_id,
        ),
    )
    conn.commit()
    conn.close()


def insert_students(
    app_module,
    session_id: str,
    students: Sequence[tuple[str, str, str, str, int | None, int | None]],
) -> None:
    rows = []
    for idx, (internal_id, public_id, name, anonymous_id, group_no, group_pos) in enumerate(students, start=1):
        rows.append(
            (
                internal_id,
                session_id,
                public_id,
                name,
                anonymous_id,
                f"2026-03-08T09:{idx:02d}:00",
                group_no,
                group_pos,
            )
        )

    conn = sqlite3.connect(app_module.DB_PATH)
    conn.executemany(
        """
        INSERT INTO students(id, session_id, student_id, name, anonymous_id, joined_at, group_no, group_pos)
        VALUES(?,?,?,?,?,?,?,?)
    """,
        rows,
    )
    conn.commit()
    conn.close()


def insert_contributions(app_module, session_id: str, round_no: int, contributions: dict[str, int]) -> None:
    rows = []
    for idx, (student_internal_id, contrib) in enumerate(contributions.items(), start=1):
        rows.append(
            (
                session_id,
                round_no,
                student_internal_id,
                contrib,
                f"2026-03-08T10:{idx:02d}:00",
            )
        )

    conn = sqlite3.connect(app_module.DB_PATH)
    conn.executemany(
        """
        INSERT INTO contributions(session_id, round_no, student_id, contrib, created_at)
        VALUES(?,?,?,?,?)
    """,
        rows,
    )
    conn.commit()
    conn.close()


def insert_actions(
    app_module,
    session_id: str,
    round_no: int,
    actions: Sequence[tuple[str, str, int]],
) -> None:
    rows = []
    for idx, (actor_student_id, target_student_id, points) in enumerate(actions, start=1):
        rows.append(
            (
                session_id,
                round_no,
                actor_student_id,
                target_student_id,
                points,
                f"2026-03-08T11:{idx:02d}:00",
            )
        )

    conn = sqlite3.connect(app_module.DB_PATH)
    conn.executemany(
        """
        INSERT INTO actions(session_id, round_no, actor_student_id, target_student_id, points, created_at)
        VALUES(?,?,?,?,?,?)
    """,
        rows,
    )
    conn.commit()
    conn.close()


def insert_results(
    app_module,
    session_id: str,
    round_no: int,
    results: Sequence[tuple[str, int, int, int, float, int, str, int, int, int, float, float, float, float, float]],
) -> None:
    rows = []
    for idx, (
        student_internal_id,
        group_no,
        group_n,
        group_total,
        public_return,
        contrib,
        phase,
        phase_round,
        action_sent,
        action_received,
        action_cost,
        action_effect,
        income,
        cumulative,
        phase_cumulative,
    ) in enumerate(results, start=1):
        rows.append(
            (
                session_id,
                round_no,
                student_internal_id,
                group_no,
                group_n,
                group_total,
                public_return,
                contrib,
                phase,
                phase_round,
                action_sent,
                action_received,
                action_cost,
                action_effect,
                income,
                cumulative,
                phase_cumulative,
                f"2026-03-08T12:{idx:02d}:00",
            )
        )

    conn = sqlite3.connect(app_module.DB_PATH)
    conn.executemany(
        """
        INSERT INTO results(
            session_id, round_no, student_id, group_no, group_n, group_total,
            public_return, contrib, phase, phase_round, action_sent, action_received,
            action_cost, action_effect, income, cumulative, phase_cumulative, computed_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        rows,
    )
    conn.commit()
    conn.close()
