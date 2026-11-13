"""Shared SQLite connection and schema.

The previous code opened and closed a new connection on every cache read and
write. This keeps one connection for the process (SQLite handles this fine with
`check_same_thread=False` plus a lock) and enables WAL so a write never blocks a
read. The schema lives here so the runtime and `scripts/setup_db.py` can never
drift apart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assessment_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL UNIQUE COLLATE NOCASE,
    total_value TEXT,
    year_built TEXT,
    number_of_bed TEXT,
    number_of_baths TEXT,
    description TEXT,
    land_size TEXT,
    strata_area TEXT,
    building_storeys TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_cache_address ON assessment_cache(address);

-- Failed / incomplete lookups. Doubles as the retry queue and, for
-- failure_type='not_found', as a negative cache so unknown addresses stop
-- burning the BC Assessment rate-limit budget on every request.
CREATE TABLE IF NOT EXISTS failed_lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    normalized_address TEXT NOT NULL UNIQUE COLLATE NOCASE,
    failure_type TEXT NOT NULL,
    reason TEXT,
    step TEXT,
    url TEXT,
    html_snapshot_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    next_retry_at TEXT,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_failed_lookups_norm
    ON failed_lookups(normalized_address);
CREATE INDEX IF NOT EXISTS idx_failed_lookups_retry
    ON failed_lookups(resolved_at, next_retry_at);

-- Batch lookups (POST /batch). One row per job, one row per submitted address
-- (duplicates included, so every original index has a row); items sharing an
-- address_key are resolved by a single scrape.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    total INTEGER NOT NULL,
    client_ref TEXT,
    callback_url TEXT,
    paused_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_batch_jobs_state ON batch_jobs(state, created_at);

CREATE TABLE IF NOT EXISTS batch_items (
    job_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    address_raw TEXT NOT NULL,
    address_key TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT,
    error_type TEXT,
    source TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (job_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_batch_items_due
    ON batch_items(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_batch_items_job_idx
    ON batch_items(job_id, idx);
CREATE INDEX IF NOT EXISTS idx_batch_items_key
    ON batch_items(address_key, state);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def db_path() -> Path:
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent.parent / path
    return path


def init_db(path: Path | None = None) -> Path:
    """Create the database file and apply the schema. Idempotent."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        # WAL is persistent in the file, so setting it here covers every later
        # connection (batch writes one row per ~4 s alongside live reads).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return target


def get_connection() -> sqlite3.Connection:
    """Return the shared connection, creating the database if it is missing."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn

        path = db_path()
        if not path.exists():
            logger.info("Cache database missing; creating %s", path)
            init_db(path)

        conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        # Applying the schema here keeps older databases (created before
        # failed_lookups existed) working without a manual migration step.
        conn.executescript(SCHEMA)
        conn.commit()
        _conn = conn
        return _conn


def close_connection() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


def reset_for_tests() -> None:
    """Drop the cached connection so a test can point at a new database."""
    close_connection()
