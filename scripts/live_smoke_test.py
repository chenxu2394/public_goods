#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import getpass
import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request


USER_AGENT = "public-goods-live-smoke-test/1.0"
AUTH_COOKIE_NAME = "pg_auth"


@dataclass(frozen=True)
class Student:
    student_id: str
    name: str


@dataclass
class AttemptResult:
    ok: bool
    status: int | None
    detail: str
    elapsed_ms: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a one-person live smoke test against the deployed student endpoints. "
            "The script generates a whitelist CSV, joins mock students via the public join link, "
            "waits for your admin step, then fires a concurrent contribution burst."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("full", "prepare", "submit"),
        default="full",
        help=(
            "Smoke-test mode. "
            "'full' uploads whitelist, joins students, and submits contributions. "
            "'prepare' stops after the join burst. "
            "'submit' skips joining and runs only the contribution burst. Default: full"
        ),
    )
    parser.add_argument(
        "--join-url",
        default="",
        help="Public join URL, for example https://public-goods.azurewebsites.net/join/AbCdEf12",
    )
    parser.add_argument(
        "--session-url",
        default="",
        help="Optional admin session URL, for example https://public-goods.azurewebsites.net/admin/AbCdEf12",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Optional raw session id. Use this or --session-url for automatic admin preparation.",
    )
    parser.add_argument(
        "--username",
        default="",
        help="Optional management username (admin or owner teacher) for automatic whitelist upload and round setup.",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Optional management password. If omitted, the script uses PG_SMOKE_PASSWORD or prompts securely.",
    )
    parser.add_argument(
        "--state-file",
        default="",
        help=(
            "Optional JSON state file. "
            "In prepare/full mode the script writes joined-student metadata here. "
            "In submit mode the script can load the existing roster from this file."
        ),
    )
    parser.add_argument(
        "--students",
        type=int,
        default=10,
        help="Number of mock students to simulate. Default: 10",
    )
    parser.add_argument(
        "--join-batch-size",
        type=int,
        default=0,
        help=(
            "Optional join batch size for full/prepare mode. "
            "0 means one full join burst. "
            "Use a smaller value such as 10 to populate the roster in waves before a separate submit-only test."
        ),
    )
    parser.add_argument(
        "--student-id-prefix",
        default="SMOKE",
        help="Prefix for generated student IDs. Default: SMOKE",
    )
    parser.add_argument(
        "--name-prefix",
        default="Smoke Test",
        help='Prefix for generated names. Default: "Smoke Test"',
    )
    parser.add_argument(
        "--contrib",
        type=int,
        default=5,
        help="Contribution value to submit for every student. Default: 5",
    )
    parser.add_argument(
        "--pollers",
        type=int,
        default=-1,
        help=(
            "Background status pollers to run during the submit burst. "
            "Use 0 to disable. Default: one per student"
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between background status polls. Default: 2.0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-request timeout in seconds. Default: 15.0",
    )
    parser.add_argument(
        "--whitelist-out",
        default="",
        help="Optional path for the generated whitelist CSV. Default: a temp file under /tmp",
    )
    parser.add_argument(
        "--with-actions",
        action="store_true",
        help="After the contribution burst, also wait for the admin action stage and fire action submits.",
    )
    return parser.parse_args()


def make_students(count: int, student_id_prefix: str, name_prefix: str) -> list[Student]:
    width = max(3, len(str(count)))
    return [
        Student(
            student_id=f"{student_id_prefix}{index:0{width}d}",
            name=f"{name_prefix} {index:0{width}d}",
        )
        for index in range(1, count + 1)
    ]


def write_whitelist_csv(students: list[Student], out_path: str) -> Path:
    if out_path:
        path = Path(out_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="public_goods_smoke_", suffix=".csv", dir="/tmp")
        os.close(fd)
        path = Path(tmp_name)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["student_id", "name"])
        for student in students:
            writer.writerow([student.student_id, student.name])
    return path


def prompt(message: str) -> None:
    try:
        input(message)
    except EOFError as exc:
        raise SystemExit("Interactive prompt aborted.") from exc


def clip_text(text: str, limit: int = 180) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def format_elapsed(values_ms: list[int]) -> str:
    if not values_ms:
        return "n/a"
    ordered = sorted(values_ms)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return f"min={ordered[0]}ms avg={sum(ordered) // len(ordered)}ms p95={ordered[p95_index]}ms max={ordered[-1]}ms"


def join_token_from_url(join_url: str) -> str:
    parsed = parse.urlsplit(join_url)
    path = parsed.path.rstrip("/")
    if "/join/" not in path:
        raise SystemExit(f"Join URL must look like .../join/<token>, got: {join_url}")
    token = path.split("/join/", 1)[1]
    if not token:
        raise SystemExit(f"Join URL must look like .../join/<token>, got: {join_url}")
    return token


def site_root_from_url(url: str) -> str:
    parsed = parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"URL must include scheme and host, got: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def session_id_from_session_url(session_url: str) -> str:
    parsed = parse.urlsplit(session_url)
    path = parsed.path.rstrip("/")
    parts = path.split("/")
    if len(parts) < 3 or parts[-2] != "admin" or not parts[-1]:
        raise SystemExit(f"Session URL must look like .../admin/<session_id>, got: {session_url}")
    return parts[-1]


def resolve_management_password(username: str, password: str) -> str:
    if not username:
        return ""
    if password:
        return password
    env_password = os.environ.get("PG_SMOKE_PASSWORD", "")
    if env_password:
        return env_password
    return getpass.getpass(f"Password for {username}: ")


def load_state_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"State file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"State file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"State file must contain a JSON object: {path}")
    return data


def save_state_file(
    path: Path,
    *,
    mode: str,
    site_root: str,
    join_url: str,
    join_token: str,
    session_id: str,
    admin_url: str,
    students: list[Student],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "site_root": site_root,
        "join_url": join_url,
        "join_token": join_token,
        "session_id": session_id,
        "admin_url": admin_url,
        "students": [{"student_id": student.student_id, "name": student.name} for student in students],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def default_state_file_path(join_token: str, session_id: str) -> Path:
    return Path("/tmp") / f"public_goods_smoke_state_{join_token}_{session_id}.json"


def students_from_state(state: dict[str, Any]) -> list[Student]:
    raw_students = state.get("students")
    if not isinstance(raw_students, list) or len(raw_students) == 0:
        raise SystemExit("State file does not contain any students.")
    students: list[Student] = []
    for row in raw_students:
        if not isinstance(row, dict):
            raise SystemExit("State file contains an invalid student entry.")
        student_id = str(row.get("student_id", "")).strip()
        name = str(row.get("name", "")).strip()
        if not student_id:
            raise SystemExit("State file contains a student without student_id.")
        students.append(Student(student_id=student_id, name=name))
    return students


def request_bytes(
    url: str,
    *,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
    opener: request.OpenerDirector | None = None,
) -> tuple[int, bytes, str]:
    req = request.Request(url=url, data=data, headers=headers, method=method)
    opener_to_use = opener.open if opener is not None else request.urlopen
    with opener_to_use(req, timeout=timeout) as resp:
        return int(resp.status), resp.read(), resp.geturl()


def post_form(
    url: str,
    form_data: dict[str, str],
    timeout: float,
    *,
    opener: request.OpenerDirector | None = None,
) -> tuple[int, bytes, str]:
    data = parse.urlencode(form_data).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }
    return request_bytes(url, method="POST", data=data, headers=headers, timeout=timeout, opener=opener)


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    opener: request.OpenerDirector | None = None,
) -> tuple[int, bytes, str]:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    return request_bytes(url, method="POST", data=data, headers=headers, timeout=timeout, opener=opener)


def encode_multipart_form(
    *,
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
) -> tuple[str, bytes]:
    boundary = f"----publicgoods{uuid.uuid4().hex}"
    boundary_bytes = boundary.encode("utf-8")
    body: list[bytes] = []

    for key, value in fields.items():
        body.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    for field_name, filename, content_type, content in files:
        body.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                content,
                b"\r\n",
            ]
        )

    body.append(b"--" + boundary_bytes + b"--\r\n")
    return boundary, b"".join(body)


def post_multipart(
    url: str,
    *,
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
    timeout: float,
    opener: request.OpenerDirector | None = None,
) -> tuple[int, bytes, str]:
    boundary, data = encode_multipart_form(fields=fields, files=files)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": USER_AGENT,
    }
    return request_bytes(url, method="POST", data=data, headers=headers, timeout=timeout, opener=opener)


def get_json(url: str, timeout: float, *, opener: request.OpenerDirector | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    status, body, _ = request_bytes(url, method="GET", data=None, headers=headers, timeout=timeout, opener=opener)
    if status < 200 or status >= 300:
        raise RuntimeError(f"Unexpected status {status} from {url}")
    return json.loads(body.decode("utf-8"))


def response_body(exc: error.HTTPError) -> str:
    return clip_text(exc.read().decode("utf-8", errors="replace"))


def login_management_user(
    site_root: str,
    username: str,
    password: str,
    timeout: float,
) -> request.OpenerDirector:
    cookie_jar = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cookie_jar))
    login_url = f"{site_root}/admin/login"
    try:
        _, _, final_url = post_form(
            login_url,
            {"username": username, "password": password},
            timeout,
            opener=opener,
        )
    except error.HTTPError as exc:
        raise RuntimeError(f"Login failed with HTTP {exc.code}: {response_body(exc)}") from exc

    if not any(cookie.name == AUTH_COOKIE_NAME for cookie in cookie_jar):
        raise RuntimeError("Login did not produce an authenticated session cookie.")

    final_parts = parse.urlsplit(final_url)
    if parse.parse_qs(final_parts.query).get("pw_change_required") == ["1"]:
        raise RuntimeError("This account must change password before it can use admin endpoints.")

    return opener


def upload_whitelist(
    site_root: str,
    session_id: str,
    whitelist_path: Path,
    timeout: float,
    opener: request.OpenerDirector,
) -> None:
    url = f"{site_root}/admin/{parse.quote(session_id)}/whitelist/upload"
    try:
        post_multipart(
            url,
            fields={},
            files=[("file", whitelist_path.name, "text/csv; charset=utf-8", whitelist_path.read_bytes())],
            timeout=timeout,
            opener=opener,
        )
    except error.HTTPError as exc:
        raise RuntimeError(f"Whitelist upload failed with HTTP {exc.code}: {response_body(exc)}") from exc


def post_admin_action(
    site_root: str,
    session_id: str,
    action: str,
    timeout: float,
    opener: request.OpenerDirector,
    form_data: dict[str, str] | None = None,
) -> None:
    url = f"{site_root}/admin/{parse.quote(session_id)}/{action}"
    try:
        post_form(url, form_data or {}, timeout, opener=opener)
    except error.HTTPError as exc:
        raise RuntimeError(f"Admin action {action!r} failed with HTTP {exc.code}: {response_body(exc)}") from exc


def stage_from_status_payload(status_payload: dict[str, Any]) -> str:
    return str(status_payload.get("session", {}).get("stage", "")).strip()


def extract_session_id(student_url: str, student_id: str) -> str:
    path = parse.urlsplit(student_url).path.rstrip("/")
    parts = path.split("/")
    if len(parts) < 4 or parts[-3] != "s" or parts[-1] != student_id:
        raise RuntimeError(f"Unexpected redirect target for {student_id}: {student_url}")
    return parts[-2]


def run_burst(
    label: str,
    items: list[Any],
    worker: Callable[[Any], Any],
    attempt_result: Callable[[Any], AttemptResult],
) -> list[Any]:
    start_gate = threading.Event()
    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        futures = [pool.submit(run_worker, start_gate, worker, item) for item in items]
        time.sleep(0.15)
        started_at = time.monotonic()
        start_gate.set()
        for future in as_completed(futures):
            results.append(future.result())
    total_ms = int((time.monotonic() - started_at) * 1000)
    attempts = [attempt_result(result) for result in results]
    ok_count = sum(1 for result in attempts if result.ok)
    print(f"{label}: {ok_count}/{len(items)} ok in {total_ms}ms ({format_elapsed([r.elapsed_ms for r in attempts])})")
    failures = [result for result in attempts if not result.ok]
    for failure in failures[:5]:
        print(f"  failure: {failure.detail}")
    return results


def chunk_items(items: list[Any], chunk_size: int) -> list[list[Any]]:
    if chunk_size <= 0 or chunk_size >= len(items):
        return [items]
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def run_burst_in_chunks(
    label: str,
    items: list[Any],
    worker: Callable[[Any], Any],
    attempt_result: Callable[[Any], AttemptResult],
    chunk_size: int,
) -> list[Any]:
    chunks = chunk_items(items, chunk_size)
    if len(chunks) == 1:
        return run_burst(label, items, worker, attempt_result)

    results: list[Any] = []
    for index, chunk in enumerate(chunks, start=1):
        print(f"{label}: starting chunk {index}/{len(chunks)} ({len(chunk)} items)")
        chunk_results = run_burst(f"{label} chunk {index}/{len(chunks)}", chunk, worker, attempt_result)
        results.extend(chunk_results)
    return results


def run_worker(
    start_gate: threading.Event,
    worker: Callable[[Any], Any],
    item: Any,
) -> Any:
    start_gate.wait()
    return worker(item)


def join_one(join_url: str, student: Student, timeout: float) -> tuple[Student, AttemptResult, str]:
    started = time.monotonic()
    try:
        _, _, final_url = post_form(
            join_url,
            {"student_id": student.student_id, "name": student.name},
            timeout=timeout,
        )
        session_id = extract_session_id(final_url, student.student_id)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return student, AttemptResult(True, 200, f"{student.student_id} joined", elapsed_ms), session_id
    except error.HTTPError as exc:
        body = clip_text(exc.read().decode("utf-8", errors="replace"))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        detail = f"{student.student_id} join -> HTTP {exc.code}: {body}"
        return student, AttemptResult(False, exc.code, detail, elapsed_ms), ""
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return student, AttemptResult(False, None, f"{student.student_id} join -> {exc}", elapsed_ms), ""


def fetch_status(site_root: str, session_id: str, student_id: str, timeout: float) -> dict[str, Any]:
    url = (
        f"{site_root}/api/{parse.quote(session_id)}/status"
        f"?student_id={parse.quote(student_id)}"
    )
    return get_json(url, timeout=timeout)


def submit_contribution(
    site_root: str,
    session_id: str,
    student: Student,
    contrib: int,
    timeout: float,
) -> AttemptResult:
    started = time.monotonic()
    url = f"{site_root}/api/{parse.quote(session_id)}/submit"
    try:
        status, body, _ = post_form(
            url,
            {"student_id": student.student_id, "contrib": str(contrib)},
            timeout=timeout,
        )
        payload = json.loads(body.decode("utf-8"))
        ok = bool(payload.get("ok")) and int(payload.get("contrib", -1)) == contrib
        detail = f"{student.student_id} submit -> HTTP {status}"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(ok, status, detail, elapsed_ms)
    except error.HTTPError as exc:
        body = clip_text(exc.read().decode("utf-8", errors="replace"))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(False, exc.code, f"{student.student_id} submit -> HTTP {exc.code}: {body}", elapsed_ms)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(False, None, f"{student.student_id} submit -> {exc}", elapsed_ms)


def submit_actions(
    site_root: str,
    session_id: str,
    student: Student,
    allocations: dict[str, int],
    timeout: float,
) -> AttemptResult:
    started = time.monotonic()
    url = f"{site_root}/api/{parse.quote(session_id)}/submit_actions"
    try:
        status, body, _ = post_json(
            url,
            {"student_id": student.student_id, "allocations": allocations},
            timeout=timeout,
        )
        payload = json.loads(body.decode("utf-8"))
        ok = bool(payload.get("ok"))
        detail = f"{student.student_id} action submit -> HTTP {status}"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(ok, status, detail, elapsed_ms)
    except error.HTTPError as exc:
        body = clip_text(exc.read().decode("utf-8", errors="replace"))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(False, exc.code, f"{student.student_id} action submit -> HTTP {exc.code}: {body}", elapsed_ms)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return AttemptResult(False, None, f"{student.student_id} action submit -> {exc}", elapsed_ms)


def verify_submitted_contributions(
    site_root: str,
    session_id: str,
    students: list[Student],
    expected_contrib: int,
    timeout: float,
) -> list[str]:
    errors: list[str] = []
    for student in students:
        try:
            status_payload = fetch_status(site_root, session_id, student.student_id, timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{student.student_id} verify -> {exc}")
            continue
        current_round = status_payload.get("current_round", {})
        submitted = current_round.get("submitted_contrib")
        if submitted != expected_contrib:
            errors.append(f"{student.student_id} verify -> expected contrib {expected_contrib}, got {submitted}")
    return errors


def build_action_allocations(status_payload: dict[str, Any]) -> dict[str, int]:
    allocations: dict[str, int] = {}
    for target in status_payload.get("action_targets", []):
        anon_id = str(target.get("anonymous_id", "")).strip()
        if anon_id:
            allocations[anon_id] = 1
    return allocations


def poll_status_loop(
    stop_event: threading.Event,
    site_root: str,
    session_id: str,
    student_id: str,
    timeout: float,
    interval: float,
    errors_out: list[str],
    errors_lock: threading.Lock,
) -> None:
    while not stop_event.is_set():
        try:
            fetch_status(site_root, session_id, student_id, timeout)
        except Exception as exc:  # noqa: BLE001
            with errors_lock:
                errors_out.append(f"{student_id} poll -> {exc}")
        stop_event.wait(interval)


def start_pollers(
    students: list[Student],
    site_root: str,
    session_id: str,
    timeout: float,
    interval: float,
    poller_count: int,
) -> tuple[threading.Event, list[threading.Thread], list[str]]:
    stop_event = threading.Event()
    errors: list[str] = []
    errors_lock = threading.Lock()
    threads: list[threading.Thread] = []
    for student in students[:poller_count]:
        thread = threading.Thread(
            target=poll_status_loop,
            args=(
                stop_event,
                site_root,
                session_id,
                student.student_id,
                timeout,
                interval,
                errors,
                errors_lock,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return stop_event, threads, errors


def stop_pollers(stop_event: threading.Event, threads: list[threading.Thread]) -> None:
    stop_event.set()
    for thread in threads:
        thread.join(timeout=3.0)


def print_students(students: list[Student]) -> None:
    for student in students:
        print(f"  {student.student_id},{student.name}")


def main() -> int:
    args = parse_args()
    if args.students < 1:
        raise SystemExit("--students must be at least 1")
    if args.pollers < -1:
        raise SystemExit("--pollers must be -1, 0, or a positive integer")
    if args.join_batch_size < 0:
        raise SystemExit("--join-batch-size must be 0 or a positive integer")

    mode = args.mode
    state_file_arg = args.state_file.strip()
    state_path = Path(state_file_arg).expanduser().resolve() if state_file_arg else None
    loaded_state: dict[str, Any] = {}
    if mode == "submit" and state_path is not None:
        loaded_state = load_state_file(state_path)

    join_url = args.join_url.strip().rstrip("/")
    if not join_url and isinstance(loaded_state.get("join_url"), str):
        join_url = str(loaded_state["join_url"]).strip().rstrip("/")

    session_url = args.session_url.strip()
    if not session_url and isinstance(loaded_state.get("admin_url"), str):
        session_url = str(loaded_state["admin_url"]).strip()

    site_root_source = join_url or session_url or str(loaded_state.get("site_root", "")).strip()
    if not site_root_source:
        raise SystemExit("Provide --join-url, --session-url, or --state-file so the script knows which site to target.")
    site_root = site_root_from_url(site_root_source)

    if mode in {"full", "prepare"} and not join_url:
        raise SystemExit("--join-url is required in full and prepare modes")

    join_token = ""
    if join_url:
        join_token = join_token_from_url(join_url)
    elif isinstance(loaded_state.get("join_token"), str):
        join_token = str(loaded_state["join_token"]).strip()

    requested_session_id = args.session_id.strip() or str(loaded_state.get("session_id", "")).strip()
    if session_url:
        session_url_root = site_root_from_url(session_url)
        if session_url_root != site_root:
            raise SystemExit("--session-url must use the same scheme and host as --join-url or --state-file")
        session_url_session_id = session_id_from_session_url(session_url)
        if requested_session_id and requested_session_id != session_url_session_id:
            raise SystemExit("--session-id and --session-url refer to different sessions")
        requested_session_id = session_url_session_id

    username = args.username.strip()
    password = resolve_management_password(username, args.password)
    if password and not username:
        raise SystemExit("--password requires --username")
    if username and not requested_session_id:
        raise SystemExit("--username requires --session-id, --session-url, or a --state-file containing session_id")

    if mode == "submit" and not requested_session_id:
        raise SystemExit("submit mode requires --session-id, --session-url, or a --state-file from a previous prepare/full run")

    if mode == "submit" and loaded_state:
        students = students_from_state(loaded_state)
    else:
        students = make_students(args.students, args.student_id_prefix, args.name_prefix)

    whitelist_path: Path | None = None
    if mode in {"full", "prepare"}:
        whitelist_path = write_whitelist_csv(students, args.whitelist_out)

    poller_count = len(students) if args.pollers == -1 else min(args.pollers, len(students))
    management_opener: request.OpenerDirector | None = None

    print(f"Mode: {mode}")
    if join_url:
        print(f"Join URL: {join_url}")
    if join_token:
        print(f"Join token: {join_token}")
    if state_path is not None:
        print(f"State file: {state_path}")
    if mode in {"full", "prepare"} and whitelist_path is not None:
        print(f"Generated whitelist CSV: {whitelist_path}")
        print("Generated whitelist rows:")
        print_students(students)
        print()
    elif mode == "submit":
        print(f"Loaded student roster size: {len(students)}")
        print(f"Student ID sample: {students[0].student_id} .. {students[-1].student_id}")
        print()

    if mode in {"full", "prepare"}:
        if username:
            print(f"Management automation: enabled for user {username} and session {requested_session_id}")
            try:
                management_opener = login_management_user(site_root, username, password, args.timeout)
                upload_whitelist(site_root, requested_session_id, whitelist_path, args.timeout, management_opener)
            except RuntimeError as exc:
                print(f"Automatic admin preparation failed: {exc}")
                return 1
            print("Whitelist uploaded automatically.")
        else:
            prompt(
                "Upload the generated CSV to the session whitelist in admin, keep the join link valid, "
                "then press Enter to start the join burst..."
            )

        join_results: list[tuple[Student, AttemptResult, str]] = run_burst_in_chunks(
            "join burst",
            students,
            lambda student: join_one(join_url, student, args.timeout),
            lambda joined: joined[1],
            args.join_batch_size,
        )

        failed_joins = [result for _, result, _ in join_results if not result.ok]
        if failed_joins:
            print("Join burst failed. Use a fresh session or verify the whitelist rows match exactly.")
            return 1

        session_ids = {session_id for _, _, session_id in join_results if session_id}
        if len(session_ids) != 1:
            print(f"Expected exactly one session_id after join burst, got: {sorted(session_ids)}")
            return 1
        session_id = next(iter(session_ids))
        if requested_session_id and requested_session_id != session_id:
            print(f"Join burst reached session {session_id}, but requested session was {requested_session_id}.")
            return 1
        admin_url = f"{site_root}/admin/{session_id}"

        if state_path is None:
            state_path = default_state_file_path(join_token or "session", session_id)
        save_state_file(
            state_path,
            mode=mode,
            site_root=site_root,
            join_url=join_url,
            join_token=join_token,
            session_id=session_id,
            admin_url=admin_url,
            students=students,
        )

        print()
        print(f"Session id: {session_id}")
        print(f"Admin panel: {admin_url}")
        print(f"Saved state file: {state_path}")

        if mode == "prepare":
            print("Prepare step passed. Students are joined, but no contribution burst was run.")
            print("Next command:")
            print(
                f"  python3 scripts/live_smoke_test.py --mode submit --state-file {state_path}"
                + (f" --username {username}" if username else "")
            )
            return 0
    else:
        session_id = requested_session_id
        admin_url = f"{site_root}/admin/{session_id}"
        print(f"Session id: {session_id}")
        print(f"Admin panel: {admin_url}")
        if loaded_state:
            print("Using joined student roster from state file.")
        print()

    if management_opener is None and username:
        print(f"Management automation: enabled for user {username} and session {session_id}")
        try:
            management_opener = login_management_user(site_root, username, password, args.timeout)
        except RuntimeError as exc:
            print(f"Automatic management login failed: {exc}")
            return 1

    if management_opener is not None:
        try:
            post_admin_action(site_root, session_id, "lock", args.timeout, management_opener)
        except RuntimeError as exc:
            print(f"Automatic group lock failed: {exc}")
            return 1
        try:
            sample_status = fetch_status(site_root, session_id, students[0].student_id, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to read student status after auto-lock: {exc}")
            return 1

        stage = stage_from_status_payload(sample_status)
        if stage == "closed":
            try:
                post_admin_action(site_root, session_id, "open_round", args.timeout, management_opener)
            except RuntimeError as exc:
                print(f"Automatic round preparation failed: {exc}")
                return 1
            try:
                sample_status = fetch_status(site_root, session_id, students[0].student_id, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"Failed to read student status after opening the round: {exc}")
                return 1
            stage = stage_from_status_payload(sample_status)

        if stage != "contribution":
            print(
                "Automatic round preparation stopped because the session is not in contribution stage. "
                f"Current stage: {stage!r}. Use a fresh session or reset this one before rerunning."
            )
            return 1
        print("Groups locked and contribution stage is ready.")
    else:
        if mode == "submit":
            print("Manual step required before submit burst:")
            print("  1. Confirm these students are already joined.")
            print("  2. Click Randomize groups and lock if needed.")
            print("  3. Click Open contribution stage if the round is still closed.")
        else:
            print("Join burst succeeded. Next admin step:")
            print("  1. Confirm the joined student count is correct.")
            print("  2. Click Randomize groups and lock.")
            print("  3. Click Open contribution stage.")
        print()
        prompt("After the contribution stage is open, press Enter to fire the concurrent submit burst...")

        try:
            sample_status = fetch_status(site_root, session_id, students[0].student_id, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to read student status before submit burst: {exc}")
            return 1

        stage = stage_from_status_payload(sample_status)
        if stage != "contribution":
            print(f"Expected contribution stage before submit burst, got stage={stage!r}")
            return 1

    stop_event: threading.Event | None = None
    threads: list[threading.Thread] = []
    poll_errors: list[str] = []
    if poller_count > 0:
        stop_event, threads, poll_errors = start_pollers(
            students,
            site_root,
            session_id,
            args.timeout,
            args.poll_interval,
            poller_count,
        )

    try:
        submit_results = run_burst(
            "contribution burst",
            students,
            lambda student: submit_contribution(site_root, session_id, student, args.contrib, args.timeout),
            lambda attempt: attempt,
        )
    finally:
        if stop_event is not None:
            stop_pollers(stop_event, threads)

    failed_submits = [result for result in submit_results if not result.ok]
    if poll_errors:
        print(f"Background poller errors: {len(poll_errors)}")
        for detail in poll_errors[:5]:
            print(f"  poll failure: {detail}")

    verify_errors = verify_submitted_contributions(
        site_root,
        session_id,
        students,
        args.contrib,
        args.timeout,
    )
    if verify_errors:
        print(f"Verification errors: {len(verify_errors)}")
        for detail in verify_errors[:5]:
            print(f"  verify failure: {detail}")

    if failed_submits or poll_errors or verify_errors:
        print("Contribution smoke test failed.")
        return 1

    print("Contribution smoke test passed.")

    if not args.with_actions:
        return 0

    print()
    print("Optional action-stage check requested.")
    print("In admin, move the session to a reward or punishment action stage, then press Enter.")
    prompt("Press Enter once the action stage is open...")

    action_payloads: list[tuple[Student, dict[str, int]]] = []
    action_stage = ""
    for student in students:
        try:
            status_payload = fetch_status(site_root, session_id, student.student_id, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to read action targets for {student.student_id}: {exc}")
            return 1
        action_stage = str(status_payload.get("session", {}).get("stage", ""))
        allocations = build_action_allocations(status_payload)
        action_payloads.append((student, allocations))

    if action_stage != "action":
        print(f"Expected action stage before action burst, got stage={action_stage!r}")
        return 1

    if poller_count > 0:
        stop_event, threads, poll_errors = start_pollers(
            students,
            site_root,
            session_id,
            args.timeout,
            args.poll_interval,
            poller_count,
        )
    else:
        stop_event = None
        threads = []
        poll_errors = []

    try:
        action_results = run_burst(
            "action burst",
            action_payloads,
            lambda item: submit_actions(site_root, session_id, item[0], item[1], args.timeout),
            lambda attempt: attempt,
        )
    finally:
        if stop_event is not None:
            stop_pollers(stop_event, threads)

    failed_actions = [result for result in action_results if not result.ok]
    if poll_errors:
        print(f"Background poller errors during action burst: {len(poll_errors)}")
        for detail in poll_errors[:5]:
            print(f"  poll failure: {detail}")

    if failed_actions or poll_errors:
        print("Action smoke test failed.")
        return 1

    print("Action smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
