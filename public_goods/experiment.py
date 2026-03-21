from __future__ import annotations

from ._experiment.grouping import _choose_group_sizes, assign_late_joiner, lock_groups
from ._experiment.lifecycle import advance_round, close_round, open_action_stage, open_round
from ._experiment.phases import (
    ensure_int,
    next_round_after_compute,
    phase_for_round,
    phase_label,
    phase_round_bounds_for_session,
    phase_round_count_for_session,
    phase_start_round,
    round_context,
    stage_of_session,
)
from ._experiment.reports import (
    build_phase_status,
    build_student_phase_report_conn,
    build_teacher_phase_reports_conn,
    count_computed_rounds,
    current_round_contrib_rows_conn,
    current_round_progress_conn,
    phase_computed_counts_conn,
)
from ._experiment.results import compute_results


__all__ = [
    "_choose_group_sizes",
    "advance_round",
    "assign_late_joiner",
    "build_phase_status",
    "build_student_phase_report_conn",
    "build_teacher_phase_reports_conn",
    "close_round",
    "compute_results",
    "count_computed_rounds",
    "current_round_contrib_rows_conn",
    "current_round_progress_conn",
    "ensure_int",
    "lock_groups",
    "next_round_after_compute",
    "open_action_stage",
    "open_round",
    "phase_computed_counts_conn",
    "phase_for_round",
    "phase_label",
    "phase_round_bounds_for_session",
    "phase_round_count_for_session",
    "phase_start_round",
    "round_context",
    "stage_of_session",
]
