from __future__ import annotations

import sqlite3
from typing import Dict, List, TypedDict

from fastapi import Request


class ShareLinkContext(TypedDict):
    join_link_enabled: bool
    join_url: str | None
    join_qr_data_uri: str | None


class LoginPageContext(TypedDict, total=False):
    request: Request
    configured: bool
    configuration_error: str
    pw_changed: bool
    easy_auth: bool
    error: str


class AdminHomeContext(TypedDict, total=False):
    request: Request
    user: sqlite3.Row
    is_admin: bool
    must_change_password: bool
    pw_change_required: bool
    easy_auth: bool
    sessions: List[sqlite3.Row]
    teachers: List[sqlite3.Row]
    pw_error: str
    teacher_error: str
    teacher_success: str
    teacher_temp_password: str
    teacher_temp_password_username: str


class SessionPanelContext(TypedDict, total=False):
    request: Request
    user: sqlite3.Row
    is_admin: bool
    sess: sqlite3.Row
    students: List[sqlite3.Row]
    counts: Dict[str, int]
    join_link_enabled: bool
    join_url: str | None
    join_qr_data_uri: str | None
    share_url: str
    export_url: str
    template_url: str
    display_url: str
    round_ctx: Dict[str, object]
    computed_rounds: int
    phase_statuses: List[Dict[str, object]]
    round_progress: Dict[str, object]
    current_round_contrib_rows: List[Dict[str, object]]
    phase_reports: List[Dict[str, object]]
    demo_default_student_count: int
    transfer_teachers: List[sqlite3.Row]
    title_error: str
    join_link_success: str
    transfer_error: str
    transfer_success: str
    demo_error: str
    demo_success: str
    round_error: str


class StudentStatusPayload(TypedDict):
    session: Dict[str, object]
    student: Dict[str, object]
    phase_statuses: List[Dict[str, object]]
    current_phase: Dict[str, object]
    current_round: Dict[str, object]
    group_view: List[Dict[str, object]]
    action_targets: List[Dict[str, object]]
    completed_phases: List[Dict[str, object]]


class DisplayStatusPayload(TypedDict):
    session: Dict[str, object]
    computed_rounds: int
    latest_computed_round: int | None
    latest_groups: List[Dict[str, object]]
    avg_series: List[Dict[str, object]]
    overall_avg_contrib: float | None
