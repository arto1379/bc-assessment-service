"""SQLite persistence for batch jobs and their items.

Everything a job needs to survive a restart lives here: each item's state,
result, attempts and next attempt time is written as soon as it changes (one
commit per resolved address), so a restart loses at most the scrape in flight.

Item state `running` is internal: it marks items claimed by the worker for the
scrape in flight. The API reports it as `pending`, and `recover()` turns it back
into `pending` on startup.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.db.connection import get_connection

logger = logging.getLogger(__name__)

# Job states
QUEUED, RUNNING, PAUSED, COMPLETED, CANCELLED = (
    "queued",
    "running",
    "paused",
    "completed",
    "cancelled",
)
ACTIVE_JOB_STATES = (QUEUED, RUNNING, PAUSED)
WORKABLE_JOB_STATES = (QUEUED, RUNNING)

# Item states (`running` is internal; see module docstring)
PENDING, IN_FLIGHT, DONE, NOT_FOUND, FAILED = (
    "pending",
    "running",
    "done",
    "not_found",
    "failed",
)
OPEN_ITEM_STATES = (PENDING, IN_FLIGHT)

# Batch writes are multi-statement read-then-write transactions; serialising
# them in-process avoids SQLITE_BUSY retries between submit, cancel and worker.
_write_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _public_item_state(state: str) -> str:
    return PENDING if state == IN_FLIGHT else state


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def create_job(
    job_id: str,
    *,
    state: str,
    client_ref: Optional[str],
    callback_url: Optional[str],
    items: list[dict[str, Any]],
    paused_reason: Optional[str] = None,
) -> None:
    """Insert a job and all of its items in one transaction."""
    now = now_iso()
    conn = get_connection()
    with _write_lock, conn:
        conn.execute(
            """
            INSERT INTO batch_jobs (id, state, total, client_ref, callback_url,
                                    paused_reason, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                state,
                len(items),
                client_ref,
                callback_url,
                paused_reason,
                now,
                now,
                now if state == COMPLETED else None,
            ),
        )
        conn.executemany(
            """
            INSERT INTO batch_items (job_id, idx, address_raw, address_key, state,
                                     result_json, error_type, source, attempts,
                                     next_attempt_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
            """,
            [
                (
                    job_id,
                    item["idx"],
                    item["address_raw"],
                    item["address_key"],
                    item["state"],
                    item.get("result_json"),
                    item.get("error_type"),
                    item.get("source"),
                    now,
                )
                for item in items
            ],
        )


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    row = get_connection().execute(
        "SELECT * FROM batch_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def job_progress(job_id: str) -> dict[str, int]:
    rows = get_connection().execute(
        "SELECT state, COUNT(*) AS n FROM batch_items WHERE job_id = ? GROUP BY state",
        (job_id,),
    ).fetchall()
    progress = {DONE: 0, NOT_FOUND: 0, FAILED: 0, PENDING: 0, CANCELLED: 0}
    for row in rows:
        key = _public_item_state(row["state"])
        progress[key] = progress.get(key, 0) + row["n"]
    return progress


def list_items(job_id: str, *, offset: int, limit: int) -> list[dict[str, Any]]:
    rows = get_connection().execute(
        """
        SELECT idx, address_raw, state, source, error_type, result_json
        FROM batch_items WHERE job_id = ?
        ORDER BY idx LIMIT ? OFFSET ?
        """,
        (job_id, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["state"] = _public_item_state(item["state"])
        items.append(item)
    return items


def active_job_count() -> int:
    row = get_connection().execute(
        f"SELECT COUNT(*) AS n FROM batch_jobs "
        f"WHERE state IN ({_placeholders(ACTIVE_JOB_STATES)})",
        ACTIVE_JOB_STATES,
    ).fetchone()
    return int(row["n"])


def pending_item_count() -> int:
    """Items still waiting for a scrape, service-wide."""
    row = get_connection().execute(
        f"""
        SELECT COUNT(*) AS n FROM batch_items i JOIN batch_jobs j ON j.id = i.job_id
        WHERE i.state IN ({_placeholders(OPEN_ITEM_STATES)})
          AND j.state IN ({_placeholders(ACTIVE_JOB_STATES)})
        """,
        (*OPEN_ITEM_STATES, *ACTIVE_JOB_STATES),
    ).fetchone()
    return int(row["n"])


def pending_key_count() -> int:
    """Distinct addresses still waiting for a scrape (what the worker will do)."""
    row = get_connection().execute(
        f"""
        SELECT COUNT(DISTINCT i.address_key) AS n
        FROM batch_items i JOIN batch_jobs j ON j.id = i.job_id
        WHERE i.state IN ({_placeholders(OPEN_ITEM_STATES)})
          AND j.state IN ({_placeholders(ACTIVE_JOB_STATES)})
        """,
        (*OPEN_ITEM_STATES, *ACTIVE_JOB_STATES),
    ).fetchone()
    return int(row["n"])


def set_job_states(
    from_states: tuple[str, ...], to_state: str, *, paused_reason: Optional[str] = None
) -> list[str]:
    """Move every job in `from_states` to `to_state`; returns the job ids moved."""
    conn = get_connection()
    with _write_lock, conn:
        ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM batch_jobs WHERE state IN ({_placeholders(from_states)})",
                from_states,
            )
        ]
        if ids:
            conn.execute(
                f"UPDATE batch_jobs SET state = ?, paused_reason = ?, updated_at = ? "
                f"WHERE id IN ({_placeholders(ids)})",
                (to_state, paused_reason, now_iso(), *ids),
            )
    return ids


def cancel_job(job_id: str) -> bool:
    """Cancel a non-terminal job. Results already produced are kept.

    An item whose scrape is in flight is cancelled too; when that scrape
    finishes its result still reaches the cache, just not this job.
    """
    now = now_iso()
    conn = get_connection()
    with _write_lock, conn:
        cur = conn.execute(
            f"""
            UPDATE batch_jobs SET state = ?, paused_reason = NULL, updated_at = ?,
                                  completed_at = ?
            WHERE id = ? AND state IN ({_placeholders(ACTIVE_JOB_STATES)})
            """,
            (CANCELLED, now, now, job_id, *ACTIVE_JOB_STATES),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            f"""
            UPDATE batch_items SET state = ?, updated_at = ?
            WHERE job_id = ? AND state IN ({_placeholders(OPEN_ITEM_STATES)})
            """,
            (CANCELLED, now, job_id, *OPEN_ITEM_STATES),
        )
    return True


def complete_finished_jobs(job_ids: Iterable[str]) -> list[str]:
    """Mark active jobs with no open items as completed; returns those ids."""
    ids = list(dict.fromkeys(job_ids))
    if not ids:
        return []
    now = now_iso()
    finished: list[str] = []
    conn = get_connection()
    with _write_lock, conn:
        for job_id in ids:
            cur = conn.execute(
                f"""
                UPDATE batch_jobs SET state = ?, paused_reason = NULL,
                                      updated_at = ?, completed_at = ?
                WHERE id = ? AND state IN ({_placeholders(ACTIVE_JOB_STATES)})
                  AND NOT EXISTS (
                      SELECT 1 FROM batch_items
                      WHERE job_id = ? AND state IN ({_placeholders(OPEN_ITEM_STATES)})
                  )
                """,
                (COMPLETED, now, now, job_id, *ACTIVE_JOB_STATES, job_id, *OPEN_ITEM_STATES),
            )
            if cur.rowcount:
                finished.append(job_id)
    return finished


def active_job_ids() -> list[str]:
    return [
        row["id"]
        for row in get_connection().execute(
            f"SELECT id FROM batch_jobs WHERE state IN ({_placeholders(ACTIVE_JOB_STATES)})",
            ACTIVE_JOB_STATES,
        )
    ]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def next_due_item(job_states: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """The next pending item whose backoff has elapsed, oldest job first."""
    row = get_connection().execute(
        f"""
        SELECT i.job_id, i.idx, i.address_raw, i.address_key, i.attempts
        FROM batch_items i JOIN batch_jobs j ON j.id = i.job_id
        WHERE j.state IN ({_placeholders(job_states)})
          AND i.state = ?
          AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= ?)
        ORDER BY j.created_at, j.id, i.idx
        LIMIT 1
        """,
        (*job_states, PENDING, now_iso()),
    ).fetchone()
    return dict(row) if row else None


def next_attempt_at(job_states: tuple[str, ...]) -> Optional[str]:
    """Earliest backoff deadline among pending items, so the worker can sleep."""
    row = get_connection().execute(
        f"""
        SELECT MIN(i.next_attempt_at) AS t
        FROM batch_items i JOIN batch_jobs j ON j.id = i.job_id
        WHERE j.state IN ({_placeholders(job_states)}) AND i.state = ?
        """,
        (*job_states, PENDING),
    ).fetchone()
    return row["t"] if row else None


def claim_key(address_key: str, job_states: tuple[str, ...]) -> list[dict[str, Any]]:
    """Claim every pending item for one address across the given jobs.

    This is what makes two jobs (or two indexes in one job) share one scrape.
    Claimed jobs that were `queued` become `running`.
    """
    now = now_iso()
    conn = get_connection()
    with _write_lock, conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT i.job_id, i.idx, i.attempts
                FROM batch_items i JOIN batch_jobs j ON j.id = i.job_id
                WHERE i.address_key = ? AND i.state = ?
                  AND j.state IN ({_placeholders(job_states)})
                """,
                (address_key, PENDING, *job_states),
            )
        ]
        if not rows:
            return []
        conn.executemany(
            "UPDATE batch_items SET state = ?, updated_at = ? WHERE job_id = ? AND idx = ?",
            [(IN_FLIGHT, now, r["job_id"], r["idx"]) for r in rows],
        )
        job_ids = list(dict.fromkeys(r["job_id"] for r in rows))
        conn.execute(
            f"UPDATE batch_jobs SET state = ?, updated_at = ? "
            f"WHERE state = ? AND id IN ({_placeholders(job_ids)})",
            (RUNNING, now, QUEUED, *job_ids),
        )
    return rows


def update_items(updates: list[dict[str, Any]]) -> None:
    """Write the outcome of one scrape to the items it was claimed for.

    Only items still `running` are touched, so a job cancelled mid-scrape keeps
    its `cancelled` items.
    """
    if not updates:
        return
    now = now_iso()
    conn = get_connection()
    with _write_lock, conn:
        conn.executemany(
            """
            UPDATE batch_items
            SET state = ?, result_json = ?, error_type = ?, source = ?,
                attempts = ?, next_attempt_at = ?, updated_at = ?
            WHERE job_id = ? AND idx = ? AND state = ?
            """,
            [
                (
                    u["state"],
                    u.get("result_json"),
                    u.get("error_type"),
                    u.get("source"),
                    u["attempts"],
                    u.get("next_attempt_at"),
                    now,
                    u["job_id"],
                    u["idx"],
                    IN_FLIGHT,
                )
                for u in updates
            ],
        )


def release_in_flight() -> int:
    """Return every in-flight item to pending (no attempt is counted).

    Safe whenever the single worker is not mid-scrape: at startup, and after a
    worker step failed unexpectedly between claiming and recording.
    """
    conn = get_connection()
    with _write_lock, conn:
        return conn.execute(
            "UPDATE batch_items SET state = ?, updated_at = ? WHERE state = ?",
            (PENDING, now_iso(), IN_FLIGHT),
        ).rowcount


def recover() -> tuple[int, int]:
    """Startup: in-flight items back to pending, paused jobs resume.

    Returns (items reset, jobs resumed). Queued and running jobs need nothing:
    the worker picks them up as it finds them.
    """
    items = release_in_flight()
    conn = get_connection()
    with _write_lock, conn:
        jobs = conn.execute(
            "UPDATE batch_jobs SET state = ?, paused_reason = NULL, updated_at = ? "
            "WHERE state = ?",
            (RUNNING, now_iso(), PAUSED),
        ).rowcount
    return items, jobs


def purge(cutoff_iso: str) -> int:
    """Delete finished jobs (and their items) older than the cutoff.

    Active jobs are never purged, however old, so no pending work is lost.
    """
    conn = get_connection()
    with _write_lock, conn:
        ids = [
            row["id"]
            for row in conn.execute(
                f"""
                SELECT id FROM batch_jobs
                WHERE state NOT IN ({_placeholders(ACTIVE_JOB_STATES)})
                  AND COALESCE(completed_at, created_at) < ?
                """,
                (*ACTIVE_JOB_STATES, cutoff_iso),
            )
        ]
        if not ids:
            return 0
        marks = _placeholders(ids)
        conn.execute(f"DELETE FROM batch_items WHERE job_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM batch_jobs WHERE id IN ({marks})", ids)
    return len(ids)


def counts() -> dict[str, dict[str, int]]:
    """Jobs and items by state, for /stats."""
    try:
        conn = get_connection()
        jobs = conn.execute(
            "SELECT state, COUNT(*) AS n FROM batch_jobs GROUP BY state"
        ).fetchall()
        items = conn.execute(
            "SELECT state, COUNT(*) AS n FROM batch_items GROUP BY state"
        ).fetchall()
    except sqlite3.Error:
        logger.exception("Failed to count batch jobs")
        return {"jobs": {}, "items": {}}
    item_counts: dict[str, int] = {}
    for row in items:
        key = _public_item_state(row["state"])
        item_counts[key] = item_counts.get(key, 0) + row["n"]
    return {"jobs": {row["state"]: row["n"] for row in jobs}, "items": item_counts}
