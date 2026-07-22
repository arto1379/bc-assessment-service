"""Retry queue for failed / incomplete lookups.

Two jobs:

1. Keep a durable record of every lookup that did not produce a usable result,
   with enough context (reason, step, URL, HTML snapshot) to investigate later
   and to retry.
2. Act as a negative cache for genuine `not_found` addresses so the service
   stops re-scraping an address BC Assessment does not know about, which would
   otherwise consume the rate-limit budget on every single request.

Invalid results are never written to `assessment_cache` — that is the point of
keeping this table separate.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings
from app.db.connection import get_connection
from app.schemas import FailureType

logger = logging.getLogger(__name__)

# Exponential backoff between retries of the same address, capped.
_BACKOFF_BASE_MINUTES = 15
_BACKOFF_MAX_MINUTES = 24 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_retry_at(attempts: int) -> str:
    minutes = min(_BACKOFF_BASE_MINUTES * (2 ** max(attempts - 1, 0)), _BACKOFF_MAX_MINUTES)
    return (_now() + timedelta(minutes=minutes)).isoformat()


def record_failure(
    address: str,
    normalized: str,
    failure: FailureType,
    *,
    reason: str | None = None,
    step: str | None = None,
    url: str | None = None,
    snapshot_path: str | None = None,
) -> None:
    """Upsert a failure, incrementing the attempt count and backing off."""
    now_iso = _now().isoformat()
    try:
        conn = get_connection()
        with conn:
            row = conn.execute(
                "SELECT attempts FROM failed_lookups WHERE normalized_address = ?",
                (normalized,),
            ).fetchone()
            attempts = (row["attempts"] + 1) if row else 1

            conn.execute(
                """
                INSERT INTO failed_lookups (
                    address, normalized_address, failure_type, reason, step, url,
                    html_snapshot_path, attempts, first_failed_at, last_failed_at,
                    next_retry_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(normalized_address) DO UPDATE SET
                    address = excluded.address,
                    failure_type = excluded.failure_type,
                    reason = excluded.reason,
                    step = excluded.step,
                    url = excluded.url,
                    html_snapshot_path = COALESCE(
                        excluded.html_snapshot_path, failed_lookups.html_snapshot_path
                    ),
                    attempts = ?,
                    last_failed_at = excluded.last_failed_at,
                    next_retry_at = excluded.next_retry_at,
                    resolved_at = NULL
                """,
                (
                    address,
                    normalized,
                    failure.value,
                    reason,
                    step,
                    url,
                    snapshot_path,
                    attempts,
                    now_iso,
                    now_iso,
                    _next_retry_at(attempts),
                    attempts,
                ),
            )
    except sqlite3.Error:
        logger.exception("Failed to record failure for address=%r", normalized)


def mark_resolved(normalized: str) -> None:
    """Clear an address from the queue after a later successful lookup."""
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE failed_lookups SET resolved_at = ? WHERE normalized_address = ?",
                (_now().isoformat(), normalized),
            )
    except sqlite3.Error:
        logger.exception("Failed to resolve failure for address=%r", normalized)


def get_negative_cache(normalized: str) -> Optional[str]:
    """Return a reason if this address is a still-fresh known `not_found`.

    Only `not_found` is cached negatively. Scraper faults (blocked, timeout,
    error) must always be retried — caching those would turn a transient outage
    into a permanent wrong answer, which is the bug we are fixing.
    """
    ttl = settings.not_found_cache_ttl_seconds
    if ttl <= 0:
        return None

    try:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT reason, last_failed_at FROM failed_lookups
            WHERE normalized_address = ?
              AND failure_type = ?
              AND resolved_at IS NULL
            """,
            (normalized, FailureType.NOT_FOUND.value),
        ).fetchone()
    except sqlite3.Error:
        logger.exception("Negative cache read failed for address=%r", normalized)
        return None

    if row is None:
        return None

    try:
        last = datetime.fromisoformat(row["last_failed_at"])
    except (TypeError, ValueError):
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    if (_now() - last).total_seconds() > ttl:
        return None

    return row["reason"] or "previously not found"


def list_failures(
    *, limit: int = 100, include_resolved: bool = False, due_only: bool = False
) -> list[dict[str, Any]]:
    """List queued failures, newest first."""
    clauses: list[str] = []
    params: list[Any] = []

    if not include_resolved:
        clauses.append("resolved_at IS NULL")
    if due_only:
        clauses.append("(next_retry_at IS NULL OR next_retry_at <= ?)")
        params.append(_now().isoformat())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    try:
        conn = get_connection()
        rows = conn.execute(
            f"""
            SELECT address, normalized_address, failure_type, reason, step, url,
                   html_snapshot_path, attempts, first_failed_at, last_failed_at,
                   next_retry_at, resolved_at
            FROM failed_lookups
            {where}
            ORDER BY last_failed_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except sqlite3.Error:
        logger.exception("Failed to list failures")
        return []

    return [dict(row) for row in rows]


def failure_counts() -> dict[str, int]:
    """Counts of unresolved failures by type, for /health and /stats."""
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT failure_type, COUNT(*) AS n FROM failed_lookups
            WHERE resolved_at IS NULL
            GROUP BY failure_type
            """
        ).fetchall()
    except sqlite3.Error:
        logger.exception("Failed to count failures")
        return {}

    return {row["failure_type"]: row["n"] for row in rows}
