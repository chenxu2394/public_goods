import csv
import io
import sqlite3

from fastapi.testclient import TestClient

from tests.support import (
    close_and_compute,
    create_session,
    get_student_rows,
    load_app,
    login,
    open_action_stage,
    open_round,
    setup_grouped_session,
    submit_contributions,
)


def test_whitelist_upload_accepts_bom_and_skips_blank_rows(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id = create_session(client, "Whitelist Upload Session")

        content = "\ufeffstudent_id,name\n20260001,Alice\n\n20260002,Bob\n,\n".encode("utf-8")
        response = client.post(
            f"/admin/{session_id}/whitelist/upload",
            files={"file": ("whitelist.csv", content, "text/csv")},
            follow_redirects=False,
        )

    assert response.status_code == 303

    conn = sqlite3.connect(app_module.DB_PATH)
    rows = conn.execute(
        "SELECT student_id, name FROM whitelist WHERE session_id=? ORDER BY student_id ASC",
        (session_id,),
    ).fetchall()
    conn.close()

    assert rows == [("20260001", "Alice"), ("20260002", "Bob")]


def test_whitelist_upload_rejects_empty_and_invalid_files(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id = create_session(client, "Invalid Upload Session")

        empty_response = client.post(
            f"/admin/{session_id}/whitelist/upload",
            files={"file": ("whitelist.csv", "student_id,name\n".encode("utf-8"), "text/csv")},
        )
        assert empty_response.status_code == 400
        assert "CSV is empty or invalid" in empty_response.text

        invalid_response = client.post(
            f"/admin/{session_id}/whitelist/upload",
            files={"file": ("whitelist.csv", b"\xff\xfe\x00", "text/csv")},
        )
        assert invalid_response.status_code == 400
        assert "Failed to parse CSV" in invalid_response.text


def test_join_rejects_missing_whitelist_and_name_mismatch(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id = create_session(client, "Join Validation Session")
        join_token = app_module.get_session(session_id)["join_token"]

        missing_whitelist = client.post(
            f"/join/{join_token}",
            data={"student_id": "20260001", "name": "Alice"},
            follow_redirects=False,
        )
        assert missing_whitelist.status_code == 403
        assert "Whitelist not imported" in missing_whitelist.text

        app_module.upsert_whitelist(session_id, [("20260001", "Alice")])

        unknown_student = client.post(
            f"/join/{join_token}",
            data={"student_id": "20269999", "name": "Alice"},
            follow_redirects=False,
        )
        assert unknown_student.status_code == 403
        assert "Student ID is not in the whitelist" in unknown_student.text

        name_mismatch = client.post(
            f"/join/{join_token}",
            data={"student_id": "20260001", "name": "alice"},
            follow_redirects=False,
        )
        assert name_mismatch.status_code == 403
        assert "Name does not match the whitelist exactly." in name_mismatch.text

        joined = client.post(
            f"/join/{join_token}",
            data={"student_id": "20260001", "name": "Alice"},
            follow_redirects=False,
        )
        assert joined.status_code == 303
        assert joined.headers["location"] == f"/s/{session_id}/20260001"


def test_late_joiner_is_assigned_without_rebalancing_existing_groups(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.random, "shuffle", lambda values: None)

    initial_students = [(f"2026{i:04d}", f"Student {i}") for i in range(1, 9)]
    late_students = [(f"2026{i:04d}", f"Student {i}") for i in range(9, 13)]

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, join_token = setup_grouped_session(
            client,
            app_module,
            "Late Join Session",
            students=initial_students,
            group_size=5,
        )
        original_rows = get_student_rows(app_module, session_id)
        original_assignments = {
            student_id: (row["group_no"], row["group_pos"])
            for student_id, row in original_rows.items()
        }

        app_module.upsert_whitelist(session_id, late_students)
        for student_id, name in late_students:
            response = client.post(
                f"/join/{join_token}",
                data={"student_id": student_id, "name": name},
                follow_redirects=False,
            )
            assert response.status_code == 303

    updated_rows = get_student_rows(app_module, session_id)

    for student_id in [student_id for student_id, _ in initial_students]:
        assert (
            updated_rows[student_id]["group_no"],
            updated_rows[student_id]["group_pos"],
        ) == original_assignments[student_id]

    assert updated_rows["20260009"]["group_no"] == 2
    assert updated_rows["20260009"]["group_pos"] == 5
    assert updated_rows["20260010"]["group_no"] == 2
    assert updated_rows["20260010"]["group_pos"] == 6
    assert updated_rows["20260011"]["group_no"] == 2
    assert updated_rows["20260011"]["group_pos"] == 7
    assert updated_rows["20260012"]["group_no"] == 3
    assert updated_rows["20260012"]["group_pos"] == 1


def test_admin_panel_renders_for_admin_and_teacher(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)
    teacher_password = "Teacher1-final-pass"

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        teacher = app_module.create_user(
            "Teacher1",
            app_module.USER_ROLE_TEACHER,
            teacher_password,
            must_change_password=False,
        )
        session_id = create_session(client, "Render Session")

        admin_panel = client.get(f"/admin/{session_id}")
        assert admin_panel.status_code == 200
        assert "Render Session" in admin_panel.text

        app_module.transfer_session_owner(session_id, str(teacher["id"]))
        assert login(client, "Teacher1", teacher_password).status_code == 303

        teacher_panel = client.get(f"/admin/{session_id}")
        assert teacher_panel.status_code == 200
        assert "Render Session" in teacher_panel.text


def test_share_link_page_and_api_payload_remain_consistent(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id = create_session(client, "Share Link Regression")
        join_token = app_module.get_session(session_id)["join_token"]

        share_page = client.get(f"/admin/{session_id}/share")
        assert share_page.status_code == 200
        assert "Student join link" in share_page.text
        assert f"/join/{join_token}" in share_page.text

        share_api = client.get(f"/api/admin/{session_id}/share_link")
        assert share_api.status_code == 200
        payload = share_api.json()

    assert payload["session"]["id"] == session_id
    assert payload["session"]["title"] == "Share Link Regression"
    assert payload["join_url"].endswith(join_token)
    assert payload["join_qr_data_uri"].startswith("data:image/svg+xml;base64,")


def test_cannot_open_action_stage_during_baseline(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(client, app_module, "Baseline Stage Session")
        open_round(client, session_id, 1)

        response = client.post(f"/admin/{session_id}/open_action_stage", follow_redirects=False)

    assert response.status_code == 400
    assert "Baseline rounds do not have an action stage." in response.text


def test_teacher_selects_and_locks_each_round_type(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(client, app_module, "Flexible Round Types", rounds=3)
        panel = client.get(f"/admin/{session_id}")
        assert panel.status_code == 200
        assert 'name="phase"' in panel.text
        assert "Choose a round type" in panel.text

        missing_phase = client.post(
            f"/admin/{session_id}/open_round",
            data={"round_no": "1"},
            follow_redirects=False,
        )
        assert missing_phase.status_code == 400
        assert app_module.get_session(session_id)["current_phase"] is None

        open_round(client, session_id, 1, "punishment")
        assert app_module.get_session(session_id)["current_phase"] == "punishment"

        change_after_open = client.post(
            f"/admin/{session_id}/open_round",
            data={"round_no": "1", "phase": "reward"},
            follow_redirects=False,
        )
        assert change_after_open.status_code == 400
        assert app_module.get_session(session_id)["current_phase"] == "punishment"

        submit_contributions(client, session_id, {"20260001": 1, "20260002": 2, "20260003": 3})
        open_action_stage(client, session_id)
        close_and_compute(client, session_id)
        app_module.init_db()
        conn = app_module.db()
        stored_phase = conn.execute(
            "SELECT DISTINCT phase FROM results WHERE session_id=? AND round_no=1",
            (session_id,),
        ).fetchone()["phase"]
        conn.close()
        assert stored_phase == "punishment"

        next_session = app_module.get_session(session_id)
        assert int(next_session["current_round"]) == 2
        assert next_session["current_phase"] is None

        open_round(client, session_id, 2, "baseline")
        status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"}).json()
        assert status["session"]["phase"] == "baseline"
        assert status["session"]["phase_round"] == 1

        submit_contributions(client, session_id, {"20260001": 0, "20260002": 0, "20260003": 0})
        close_and_compute(client, session_id)

        open_round(client, session_id, 3, "punishment")
        status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"}).json()
        assert status["session"]["phase_round"] == 2
        submit_contributions(client, session_id, {"20260001": 0, "20260002": 0, "20260003": 0})
        open_action_stage(client, session_id)
        close_and_compute(client, session_id)

        student_rows = get_student_rows(app_module, session_id)
        conn = app_module.db()
        final_row = conn.execute(
            """
            SELECT phase, phase_round, phase_cumulative
            FROM results
            WHERE session_id=? AND round_no=3 AND student_id=?
        """,
            (session_id, student_rows["20260001"]["id"]),
        ).fetchone()
        conn.close()
        assert final_row["phase"] == "punishment"
        assert int(final_row["phase_round"]) == 2
        assert float(final_row["phase_cumulative"]) == 22.0


def test_cannot_compute_reward_round_before_action_stage(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, students, _ = setup_grouped_session(client, app_module, "Reward Stage Session")

        open_round(client, session_id, 1, "reward")
        submit_contributions(
            client,
            session_id,
            {student_id: idx + 1 for idx, (student_id, _) in enumerate(students)},
        )

        response = client.post(f"/admin/{session_id}/close_and_compute", follow_redirects=False)

    assert response.status_code == 400
    assert "Open the Reward stage before computing this round." in response.text


def test_submit_endpoints_enforce_stage_boundaries(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(client, app_module, "Stage Boundary Session")

        closed_contribution = client.post(
            f"/api/{session_id}/submit",
            data={"student_id": "20260001", "contrib": "1"},
        )
        assert closed_contribution.status_code == 400
        assert "Contribution stage is not open" in closed_contribution.text

        open_round(client, session_id, 1, "reward")

        closed_action = client.post(
            f"/api/{session_id}/submit_actions",
            json={"student_id": "20260001", "allocations": {}},
        )
        assert closed_action.status_code == 400
        assert "Action stage is not open" in closed_action.text

        open_action_stage(client, session_id)

        contribution_during_action = client.post(
            f"/api/{session_id}/submit",
            data={"student_id": "20260001", "contrib": "1"},
        )
        assert contribution_during_action.status_code == 400
        assert "Contribution stage is not open" in contribution_during_action.text


def test_api_status_payload_changes_between_contribution_and_action_stage(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, students, _ = setup_grouped_session(client, app_module, "Status Payload Session")

        open_round(client, session_id, 1, "reward")
        submit_contributions(
            client,
            session_id,
            {student_id: idx + 1 for idx, (student_id, _) in enumerate(students)},
        )

        contribution_status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert contribution_status.status_code == 200
        contribution_payload = contribution_status.json()
        assert contribution_payload["session"]["phase"] == "reward"
        assert contribution_payload["session"]["phase_round"] == 1
        assert contribution_payload["session"]["stage"] == "contribution"
        assert contribution_payload["current_round"]["submitted_contrib"] == 1
        assert contribution_payload["current_round"]["group_view_visible"] is False
        assert contribution_payload["group_view"] == []
        assert contribution_payload["action_targets"] == []

        open_action_stage(client, session_id)

        action_status = client.get(f"/api/{session_id}/status", params={"student_id": "20260001"})
        assert action_status.status_code == 200
        action_payload = action_status.json()
        assert action_payload["session"]["stage"] == "action"
        assert action_payload["current_round"]["group_view_visible"] is True
        assert sorted(row["contrib"] for row in action_payload["group_view"]) == [2, 3]
        assert sorted(target["anonymous_id"] for target in action_payload["action_targets"]) == sorted(
            row["anonymous_id"] for row in action_payload["group_view"]
        )
        assert all(target["points"] == 0 for target in action_payload["action_targets"])


def test_display_status_reports_latest_group_totals_and_avg_series(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(client, app_module, "Display Session")

        open_round(client, session_id, 1)
        submit_contributions(client, session_id, {"20260001": 1, "20260002": 2, "20260003": 3})
        close_and_compute(client, session_id)

        open_round(client, session_id, 2)
        submit_contributions(client, session_id, {"20260001": 2, "20260002": 2, "20260003": 5})
        close_and_compute(client, session_id)

        response = client.get(f"/api/{session_id}/display_status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["computed_rounds"] == 2
    assert payload["latest_computed_round"] == 2
    assert payload["latest_groups"] == [
        {"group_no": 1, "group_n": 3, "group_total": 9, "avg_contrib": 3.0}
    ]
    assert payload["avg_series"] == [
        {"round": 1, "avg_contrib": 2.0},
        {"round": 2, "avg_contrib": 3.0},
    ]
    assert payload["overall_avg_contrib"] == 2.5


def test_export_includes_expected_phase_and_action_fields_per_round(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(
            client,
            app_module,
            "Export Session",
            rounds=12,
        )

        open_round(client, session_id, 1)
        submit_contributions(client, session_id, {"20260001": 4, "20260002": 5, "20260003": 6})
        close_and_compute(client, session_id)

        open_round(client, session_id, 2, "reward")
        submit_contributions(client, session_id, {"20260001": 1, "20260002": 2, "20260003": 3})
        open_action_stage(client, session_id)

        student_rows = get_student_rows(app_module, session_id)
        response = client.post(
            f"/api/{session_id}/submit_actions",
            json={
                "student_id": "20260001",
                "allocations": {str(student_rows["20260002"]["anonymous_id"]): 2},
            },
        )
        assert response.status_code == 200

        response = client.post(
            f"/api/{session_id}/submit_actions",
            json={
                "student_id": "20260002",
                "allocations": {str(student_rows["20260003"]["anonymous_id"]): 1},
            },
        )
        assert response.status_code == 200

        close_and_compute(client, session_id)

        export_response = client.get(f"/admin/{session_id}/export")

    assert export_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export_response.content.decode("utf-8-sig"))))
    assert len(rows) == 36

    rows_by_key = {(row["student_id"], int(row["round_no"])): row for row in rows}

    bob_reward = rows_by_key[("20260002", 2)]
    assert bob_reward["phase"] == "reward"
    assert bob_reward["phase_round"] == "1"
    assert bob_reward["action_sent"] == "1"
    assert bob_reward["action_received"] == "2"
    assert float(bob_reward["income"]) == 14.0
    assert float(bob_reward["phase_cumulative"]) == 14.0

    bob_future_reward = rows_by_key[("20260002", 3)]
    assert bob_future_reward["phase"] == ""
    assert bob_future_reward["phase_round"] == ""
    assert bob_future_reward["income"] == ""
    assert bob_future_reward["action_sent"] == ""
    assert bob_future_reward["action_received"] == ""
    assert bob_future_reward["phase_cumulative"] == ""


def test_export_header_order_remains_stable(monkeypatch, tmp_path):
    app_module, _ = load_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        assert login(client).status_code == 303
        session_id, _, _ = setup_grouped_session(
            client,
            app_module,
            "Header Export Session",
            rounds=1,
        )

        export_response = client.get(f"/admin/{session_id}/export")

    assert export_response.status_code == 200
    header = export_response.content.decode("utf-8-sig").splitlines()[0]
    assert header == (
        "experiment_id,anonymous_id,student_id,name,phase,phase_round,round_no,contribution,"
        "income,cumulative,phase_cumulative,action_sent,action_received"
    )
