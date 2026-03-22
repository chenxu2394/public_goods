from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import Callable, TypeVar

from ..config import (
    DB_PATH,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_JOURNAL_MODE,
    SQLITE_WRITE_RETRY_ATTEMPTS,
    SQLITE_WRITE_RETRY_BASE_DELAY_MS,
)


T = TypeVar("T")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _configure_sqlite_storage(conn: sqlite3.Connection) -> None:
    if not SQLITE_JOURNAL_MODE:
        return
    try:
        conn.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}").fetchone()
    except sqlite3.DatabaseError:
        return


def _is_locked_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database schema is locked" in msg


def _run_write_with_retry(work: Callable[[sqlite3.Connection], T]) -> T:
    attempt = 0
    while True:
        attempt += 1
        conn = db()
        try:
            result = work(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if attempt >= SQLITE_WRITE_RETRY_ATTEMPTS or not _is_locked_sqlite_error(exc):
                raise
            if SQLITE_WRITE_RETRY_BASE_DELAY_MS > 0:
                time.sleep((SQLITE_WRITE_RETRY_BASE_DELAY_MS / 1000.0) * attempt)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

