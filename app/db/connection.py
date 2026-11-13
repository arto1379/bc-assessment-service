"""SQLite connections and schema.

The original code opened and closed a new connection on every cache read and
write. This keeps one long-lived connection **per thread** (DB calls run in
`asyncio.to_thread` workers) and enables WAL so a write never blocks a read.

One connection shared by every thread is not safe: two threads querying it at
the same time can interleave, and one gets the other's row back (seen as an
IndexError in cache_get once batch submit and the worker read concurrently).
Per-thread connections are the standard sqlite3 answer; WAL gives concurrent
readers and `busy_timeout` serialises writers.

The schema lives here so the runtime and `scripts/setup_db.py` can never drift
apart.
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
_local = threading.local()
# Every connection opened, so close_connection() can close them all. Bumping
# the generation makes each thread open a fresh one on its next call.
_open: list[sqlite3.Connection] = []
_generation = 0
_schema_generation = -1


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
    """Return this thread's connection, creating the database if it is missing."""
    global _schema_generation
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == _generation:
        return conn

    with _lock:
        path = db_path()
        if not path.exists():
            logger.info("Cache database missing; creating %s", path)
            init_db(path)

        # check_same_thread=False only so close_connection() may close it from
        # another thread; queries on it come from this thread alone.
        conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        if _schema_generation != _generation:
            # Applying the schema here keeps older databases (created before
            # failed_lookups / batch tables existed) working without a manual
            # migration step. Once per generation is enough.
            conn.executescript(SCHEMA)
            conn.commit()
            _schema_generation = _generation
        _open.append(conn)
        _local.conn = conn
        _local.generation = _generation
        return conn


def close_connection() -> None:
    global _generation
    with _lock:
        for conn in _open:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _open.clear()
        _generation += 1


def reset_for_tests() -> None:
    """Drop the cached connection so a test can point at a new database."""
    close_connection()
