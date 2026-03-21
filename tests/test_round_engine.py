import pytest

from tests.support import (
    get_result_rows,
    get_student_rows,
    insert_actions,
    insert_contributions,
    insert_results,
    insert_session,
    insert_students,
    load_initialized_app,
)


def test_compute_results_baseline_reward_and_punishment_math(monkeypatch, tmp_path):
    app_module, _ = load_initialized_app(monkeypatch, tmp_path)
    session_id = "math-session"

    insert_session(app_module, session_id, multiplier=1.5, endowment=10, rounds=30)
    insert_students(
        app_module,
        session_id,
        [
            ("stu-alpha", "20260001", "Alice", "A2", 1, 1),
            ("stu-beta", "20260002", "Bob", "B3", 1, 2),
            ("stu-gamma", "20260003", "Cara", "C4", 1, 3),
        ],
    )
    student_rows = get_student_rows(app_module, session_id)
    internal_ids = {student_id: str(row["id"]) for student_id, row in student_rows.items()}

    insert_contributions(
        app_module,
        session_id,
        1,
        {
            internal_ids["20260001"]: 1,
            internal_ids["20260002"]: 2,
            internal_ids["20260003"]: 3,
        },
    )
    app_module.compute_results(session_id, 1)
    baseline_rows = get_result_rows(app_module, session_id, 1)

    assert baseline_rows["20260001"]["phase"] == "baseline"
    assert baseline_rows["20260001"]["phase_round"] == 1
    assert float(baseline_rows["20260001"]["public_return"]) == pytest.approx(3.0)
    assert float(baseline_rows["20260001"]["income"]) == pytest.approx(12.0)
    assert float(baseline_rows["20260001"]["cumulative"]) == pytest.approx(12.0)
    assert float(baseline_rows["20260001"]["phase_cumulative"]) == pytest.approx(12.0)
    assert float(baseline_rows["20260002"]["income"]) == pytest.approx(11.0)
    assert float(baseline_rows["20260003"]["income"]) == pytest.approx(10.0)

    insert_results(
        app_module,
        session_id,
        10,
        [
            (internal_ids["20260001"], 1, 3, 6, 3.0, 1, "baseline", 10, 0, 0, 0.0, 0.0, 12.0, 100.0, 100.0),
            (internal_ids["20260002"], 1, 3, 6, 3.0, 2, "baseline", 10, 0, 0, 0.0, 0.0, 11.0, 110.0, 110.0),
            (internal_ids["20260003"], 1, 3, 6, 3.0, 3, "baseline", 10, 0, 0, 0.0, 0.0, 10.0, 120.0, 120.0),
        ],
    )

    insert_contributions(
        app_module,
        session_id,
        11,
        {
            internal_ids["20260001"]: 4,
            internal_ids["20260002"]: 0,
            internal_ids["20260003"]: 1,
        },
    )
    insert_actions(
        app_module,
        session_id,
        11,
        [
            (internal_ids["20260001"], internal_ids["20260002"], 2),
            (internal_ids["20260002"], internal_ids["20260003"], 1),
        ],
    )
    app_module.compute_results(session_id, 11)
    reward_rows = get_result_rows(app_module, session_id, 11)

    assert reward_rows["20260001"]["phase"] == "reward"
    assert reward_rows["20260001"]["phase_round"] == 1
    assert reward_rows["20260001"]["action_sent"] == 2
    assert reward_rows["20260001"]["action_received"] == 0
    assert float(reward_rows["20260001"]["action_cost"]) == pytest.approx(2.0)
    assert float(reward_rows["20260001"]["action_effect"]) == pytest.approx(0.0)
    assert float(reward_rows["20260001"]["income"]) == pytest.approx(6.5)
    assert float(reward_rows["20260001"]["cumulative"]) == pytest.approx(106.5)
    assert float(reward_rows["20260001"]["phase_cumulative"]) == pytest.approx(6.5)

    assert reward_rows["20260002"]["action_sent"] == 1
    assert reward_rows["20260002"]["action_received"] == 2
    assert float(reward_rows["20260002"]["income"]) == pytest.approx(15.5)
    assert float(reward_rows["20260002"]["cumulative"]) == pytest.approx(125.5)
    assert float(reward_rows["20260002"]["phase_cumulative"]) == pytest.approx(15.5)
    assert float(reward_rows["20260003"]["income"]) == pytest.approx(13.5)
    assert float(reward_rows["20260003"]["cumulative"]) == pytest.approx(133.5)

    insert_results(
        app_module,
        session_id,
        20,
        [
            (internal_ids["20260001"], 1, 3, 5, 2.5, 4, "reward", 10, 2, 0, 2.0, 0.0, 6.5, 200.0, 60.0),
            (internal_ids["20260002"], 1, 3, 5, 2.5, 0, "reward", 10, 1, 2, 1.0, 4.0, 15.5, 210.0, 70.0),
            (internal_ids["20260003"], 1, 3, 5, 2.5, 1, "reward", 10, 0, 1, 0.0, 2.0, 13.5, 220.0, 80.0),
        ],
    )

    insert_contributions(
        app_module,
        session_id,
        21,
        {
            internal_ids["20260001"]: 0,
            internal_ids["20260002"]: 5,
            internal_ids["20260003"]: 5,
        },
    )
    insert_actions(
        app_module,
        session_id,
        21,
        [
            (internal_ids["20260001"], internal_ids["20260002"], 1),
            (internal_ids["20260003"], internal_ids["20260002"], 2),
        ],
    )
    app_module.compute_results(session_id, 21)
    punishment_rows = get_result_rows(app_module, session_id, 21)

    assert punishment_rows["20260001"]["phase"] == "punishment"
    assert punishment_rows["20260001"]["phase_round"] == 1
    assert float(punishment_rows["20260001"]["income"]) == pytest.approx(14.0)
    assert float(punishment_rows["20260001"]["cumulative"]) == pytest.approx(214.0)
    assert float(punishment_rows["20260001"]["phase_cumulative"]) == pytest.approx(14.0)

    assert punishment_rows["20260002"]["action_received"] == 3
    assert float(punishment_rows["20260002"]["action_effect"]) == pytest.approx(-9.0)
    assert float(punishment_rows["20260002"]["income"]) == pytest.approx(1.0)
    assert float(punishment_rows["20260002"]["cumulative"]) == pytest.approx(211.0)
    assert float(punishment_rows["20260002"]["phase_cumulative"]) == pytest.approx(1.0)

    assert punishment_rows["20260003"]["action_sent"] == 2
    assert float(punishment_rows["20260003"]["income"]) == pytest.approx(8.0)
    assert float(punishment_rows["20260003"]["cumulative"]) == pytest.approx(228.0)
    assert float(punishment_rows["20260003"]["phase_cumulative"]) == pytest.approx(8.0)


def test_compute_results_ignores_invalid_actions(monkeypatch, tmp_path):
    app_module, _ = load_initialized_app(monkeypatch, tmp_path)
    session_id = "invalid-action-session"

    insert_session(app_module, session_id, multiplier=1.5, endowment=10, rounds=30)
    insert_students(
        app_module,
        session_id,
        [
            ("stu-alpha", "20260001", "Alice", "A2", 1, 1),
            ("stu-beta", "20260002", "Bob", "B3", 1, 2),
            ("stu-gamma", "20260003", "Cara", "C4", 2, 1),
            ("stu-delta", "20260004", "Dan", "D5", 2, 2),
        ],
    )
    student_rows = get_student_rows(app_module, session_id)
    internal_ids = {student_id: str(row["id"]) for student_id, row in student_rows.items()}

    insert_contributions(
        app_module,
        session_id,
        11,
        {
            internal_ids["20260001"]: 0,
            internal_ids["20260002"]: 0,
            internal_ids["20260003"]: 0,
            internal_ids["20260004"]: 0,
        },
    )
    insert_actions(
        app_module,
        session_id,
        11,
        [
            (internal_ids["20260001"], internal_ids["20260001"], 5),
            (internal_ids["20260001"], internal_ids["20260003"], 4),
            (internal_ids["20260001"], internal_ids["20260002"], 3),
            (internal_ids["20260002"], internal_ids["20260001"], 0),
            (internal_ids["20260003"], internal_ids["20260004"], -1),
        ],
    )
    app_module.compute_results(session_id, 11)
    rows = get_result_rows(app_module, session_id, 11)

    assert rows["20260001"]["action_sent"] == 3
    assert rows["20260001"]["action_received"] == 0
    assert float(rows["20260001"]["action_cost"]) == pytest.approx(3.0)
    assert float(rows["20260001"]["action_effect"]) == pytest.approx(0.0)
    assert float(rows["20260001"]["income"]) == pytest.approx(7.0)

    assert rows["20260002"]["action_sent"] == 0
    assert rows["20260002"]["action_received"] == 3
    assert float(rows["20260002"]["action_cost"]) == pytest.approx(0.0)
    assert float(rows["20260002"]["action_effect"]) == pytest.approx(6.0)
    assert float(rows["20260002"]["income"]) == pytest.approx(16.0)

    assert rows["20260003"]["action_sent"] == 0
    assert rows["20260003"]["action_received"] == 0
    assert float(rows["20260003"]["income"]) == pytest.approx(10.0)

    assert rows["20260004"]["action_sent"] == 0
    assert rows["20260004"]["action_received"] == 0
    assert float(rows["20260004"]["income"]) == pytest.approx(10.0)


def test_phase_helpers_and_group_size_helpers_are_deterministic(monkeypatch, tmp_path):
    app_module, _ = load_initialized_app(monkeypatch, tmp_path)

    assert app_module.phase_for_round(1) == ("baseline", 1)
    assert app_module.phase_for_round(10) == ("baseline", 10)
    assert app_module.phase_for_round(11) == ("reward", 1)
    assert app_module.phase_for_round(20) == ("reward", 10)
    assert app_module.phase_for_round(21) == ("punishment", 1)
    assert app_module.phase_for_round(30) == ("punishment", 10)

    assert app_module.phase_round_count_for_session(8, "baseline") == 8
    assert app_module.phase_round_count_for_session(8, "reward") == 0
    assert app_module.phase_round_count_for_session(15, "reward") == 5
    assert app_module.phase_round_count_for_session(30, "punishment") == 10

    assert app_module.next_round_after_compute(1, 30) == 2
    assert app_module.next_round_after_compute(29, 30) == 30
    assert app_module.next_round_after_compute(30, 30) == 30

    assert app_module._choose_group_sizes(1, 5) == [1]
    assert app_module._choose_group_sizes(3, 5) == [3]
    assert app_module._choose_group_sizes(8, 5) == [4, 4]
    assert app_module._choose_group_sizes(11, 5) == [6, 5]
    assert app_module._choose_group_sizes(13, 5) == [5, 4, 4]

    for total in range(1, 26):
        sizes = app_module._choose_group_sizes(total, app_module.DEFAULT_GROUP_SIZE)
        assert sum(sizes) == total
        if total >= app_module.MIN_GROUP_SIZE:
            assert min(sizes) >= app_module.MIN_GROUP_SIZE
        if total > app_module.MAX_GROUP_SIZE:
            assert max(sizes) <= app_module.MAX_GROUP_SIZE
