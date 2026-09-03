from __future__ import annotations

import os

from dotenv import load_dotenv


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(PACKAGE_DIR)
load_dotenv(os.path.join(APP_DIR, ".env"), override=False)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


TEMPLATE_DIR = os.path.join(APP_DIR, "templates")
APP_TITLE = "Public Goods Experiment (Azure Easy Auth)"

# Azure Web App: /home is persistent
DEFAULT_DB_PATH = "/home/public_goods.db"
DB_PATH = os.environ.get("PUBLIC_GOODS_DB_PATH", DEFAULT_DB_PATH)
SQLITE_BUSY_TIMEOUT_MS = _env_int("SQLITE_BUSY_TIMEOUT_MS", 5000)
SQLITE_WRITE_RETRY_ATTEMPTS = _env_int("SQLITE_WRITE_RETRY_ATTEMPTS", 4, minimum=1)
SQLITE_WRITE_RETRY_BASE_DELAY_MS = _env_int("SQLITE_WRITE_RETRY_BASE_DELAY_MS", 100)
_SQLITE_ALLOWED_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
_sqlite_journal_mode_raw = (os.environ.get("SQLITE_JOURNAL_MODE") or "WAL").strip().upper()
SQLITE_JOURNAL_MODE = (
    _sqlite_journal_mode_raw
    if _sqlite_journal_mode_raw in _SQLITE_ALLOWED_JOURNAL_MODES
    else "WAL"
)

# Auth / account protection
AUTH_MODE_PASSWORD = "password"
AUTH_MODE_EASY_AUTH = "easy_auth"
_auth_mode_raw = (os.environ.get("AUTH_MODE") or AUTH_MODE_EASY_AUTH).strip().lower()
AUTH_MODE = (
    _auth_mode_raw
    if _auth_mode_raw in {AUTH_MODE_PASSWORD, AUTH_MODE_EASY_AUTH}
    else AUTH_MODE_EASY_AUTH
)
ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().casefold()
ADMIN_PASSWORD = (os.environ.get("ADMIN_PASSWORD") or "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
AUTH_COOKIE_NAME = "pg_auth"
LEGACY_ADMIN_COOKIE_NAME = "pg_admin"
AUTH_TOKEN_TTL_SECONDS = 12 * 3600
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
USER_ROLE_ADMIN = "admin"
USER_ROLE_TEACHER = "teacher"
PASSWORD_SCHEME_PBKDF2 = "pbkdf2_sha256_v1"
PASSWORD_SCHEME_LEGACY_ADMIN = "legacy_admin_secretkey_v1"
PASSWORD_SCHEME_MICROSOFT = "microsoft_easy_auth_v1"
PASSWORD_HASH_ITERATIONS = 200_000

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://public-goods.azurewebsites.net").rstrip("/")

PHASE_ROUNDS = 10
PHASES = ("baseline", "punishment", "reward")
PHASE_LABELS = {
    "baseline": "Baseline",
    "reward": "Reward",
    "punishment": "Punishment",
}
TOTAL_EXPERIMENT_ROUNDS = PHASE_ROUNDS * len(PHASES)
MIN_GROUP_SIZE = 3
MAX_GROUP_SIZE = 7
DEFAULT_GROUP_SIZE = 5

ACTION_COST = 1.0
REWARD_EFFECT = 2.0
PUNISH_EFFECT = 3.0
MAX_ACTION_POINTS = 5
DEMO_DEFAULT_STUDENT_COUNT = 24
DEMO_MAX_STUDENT_COUNT = 84
DEMO_PROFILES = ("cooperator", "conditional", "reciprocator", "free_rider")
