"""Batch lookups: cache-first submit, one background worker, circuit breaker.

Batch is a thin layer over the existing lookup path. Every address goes through
`lookup_by_address(..., batch=True)`, so the cache re-check, the fresh browser
context per lookup, block detection, the rule that invalid results are never
cached, and the `failed_lookups` record all apply unchanged. What batch adds:

* persistence (`app.db.batch`), so a restart loses at most the scrape in flight;
* one worker task that resolves one address at a time, oldest job first, with
  every pending item for that address (across jobs) sharing the one scrape;
* per-item retries with exponential backoff;
* a circuit breaker: after `BATCH_PAUSE_AFTER_BLOCKED` consecutive `blocked`
  results, active jobs pause, one Telegram alert goes out, and the worker probes
  one item at growing intervals until the scraper works again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings
from app.db import batch as store
from app.services import webhook
from app.schemas import AssessmentResult, FailureType, LookupOutcome
from app.services.browser import browser_manager
from app.services.lookup import cached_outcome, lookup_by_address, normalize_address
from app.services.rate_limit import bc_rate_limiter, seconds_per_batch_scrape
from app.services.telegram_notify import send_telegram_message_background

logger = logging.getLogger(__name__)

# Per-item retry backoff: 30 s, 60 s, 120 s, ...
RETRY_BASE_SECONDS = 30.0
# Probe interval while paused: 30 s, doubling, capped at 15 min
PROBE_BASE_SECONDS = 30.0
PROBE_MAX_SECONDS = 900.0
# How long the worker idles with nothing due before looking again
IDLE_SECONDS = 30.0
PURGE_EVERY_SECONDS = 3600.0

_RETRYABLE = frozenset({FailureType.TIMEOUT, FailureType.ERROR, FailureType.BLOCKED})


class BatchLimitError(Exception):
    """Active-job or pending-item limit reached (HTTP 429)."""


# DB calls run in threads. Cancelling the awaiting task does not stop the
# thread, so shutdown must wait for these before the shared SQLite connection
# is closed — closing it under a running query crashes the process.
_inflight_threads: set[asyncio.Future] = set()


async def _to_thread(func, *args, **kwargs):
    future = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    _inflight_threads.add(future)
    future.add_done_callback(_inflight_threads.discard)
    return await asyncio.shield(future)


async def drain_threads() -> None:
    if _inflight_threads:
        await asyncio.gather(*list(_inflight_threads), return_exceptions=True)


def _future_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _result_json(outcome: LookupOutcome) -> Optional[str]:
    return outcome.result.model_dump_json() if outcome.result is not None else None


# ---------------------------------------------------------------------------
# Submit / status / cancel
# ---------------------------------------------------------------------------

_submit_lock: Optional[asyncio.Lock] = None


def _get_submit_lock() -> asyncio.Lock:
    # Created lazily so it binds to the running loop, not the import-time one.
    global _submit_lock
    if _submit_lock is None:
        _submit_lock = asyncio.Lock()
    return _submit_lock


async def submit(
    addresses: list[str],
    *,
    client_ref: Optional[str] = None,
    callback_url: Optional[str] = None,
) -> dict[str, Any]:
    """Create a job. Addresses already in the cache resolve now (B3); the rest
    wait for the worker. Raises BatchLimitError when limits are reached."""
    rows = [(idx, raw.strip(), normalize_address(raw)) for idx, raw in enumerate(addresses)]

    unique: dict[str, str] = {}
    for _idx, raw, key in rows:
        unique.setdefault(key, raw)

    resolved: dict[str, LookupOutcome] = {}
    for key, raw in unique.items():
        hit = await cached_outcome(raw)
        if hit is not None:
            resolved[key] = hit

    to_scrape = sum(1 for _i, _r, key in rows if key not in resolved)
    keys_to_scrape = len(unique) - len(resolved)

    async with _get_submit_lock():
        estimated = 0
        if to_scrape:
            # Estimated before this job exists, so a fast worker cannot race
            # the count down to zero before we answer.
            ahead = await _to_thread(store.pending_key_count)
            estimated = _estimate_seconds(ahead + keys_to_scrape)
            active = await _to_thread(store.active_job_count)
            if active >= settings.batch_max_active_jobs:
                raise BatchLimitError(
                    f"{active} batch jobs already active "
                    f"(limit {settings.batch_max_active_jobs})"
                )
            pending = await _to_thread(store.pending_item_count)
            if pending + to_scrape > settings.batch_max_pending_items:
                raise BatchLimitError(
                    f"{pending} items already pending; this job adds {to_scrape} "
                    f"(limit {settings.batch_max_pending_items})"
                )

        items = []
        for idx, raw, key in rows:
            item: dict[str, Any] = {
                "idx": idx,
                "address_raw": raw,
                "address_key": key,
                "state": store.PENDING,
            }
            hit = resolved.get(key)
            if hit is not None:
                item.update(_terminal_fields(hit))
            items.append(item)

        job_id = secrets.token_hex(8)
        if not to_scrape:
            state = store.COMPLETED
        elif batch_worker.paused:
            state = store.PAUSED
        else:
            state = store.QUEUED
        await _to_thread(
            store.create_job,
            job_id,
            state=state,
            client_ref=client_ref,
            callback_url=callback_url,
            items=items,
            paused_reason=batch_worker.paused_reason if state == store.PAUSED else None,
        )

    logger.info(
        "batch submitted job_id=%s total=%d unique=%d cached=%d pending_scrape=%d state=%s",
        job_id,
        len(rows),
        len(unique),
        len(resolved),
        len(unique) - len(resolved),
        state,
    )

    if state == store.COMPLETED:
        await on_job_terminal(job_id)
    else:
        batch_worker.wake()

    return {
        "job_id": job_id,
        "state": state,
        "total": len(rows),
        "unique": len(unique),
        "cached": len(resolved),
        "pending_scrape": keys_to_scrape,
        "estimated_seconds": estimated,
    }


def _estimate_seconds(keys: int) -> int:
    """Lower bound from the scrapes ahead (all active jobs plus this one) and
    the batch rate share, plus any wait the limiter is already projecting."""
    seconds = keys * seconds_per_batch_scrape() + bc_rate_limiter.projected_wait(batch=True)
    return int(math.ceil(seconds))


def _terminal_fields(outcome: LookupOutcome) -> dict[str, Any]:
    """Item fields for a final cache/scrape answer (done or not_found)."""
    if outcome.ok:
        return {
            "state": store.DONE,
            "result_json": _result_json(outcome),
            "error_type": None,
            "source": outcome.source,
        }
    return {
        "state": store.NOT_FOUND,
        "result_json": None,
        "error_type": FailureType.NOT_FOUND.value,
        "source": outcome.source,
    }


async def get_status(job_id: str, *, offset: int, limit: int) -> Optional[dict[str, Any]]:
    job = await _to_thread(store.get_job, job_id)
    if job is None:
        return None
    progress = await _to_thread(store.job_progress, job_id)
    rows = await _to_thread(store.list_items, job_id, offset=offset, limit=limit)
    items = [
        {
            "index": row["idx"],
            "address": row["address_raw"],
            "state": row["state"],
            "source": row["source"],
            "error_type": row["error_type"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }
        for row in rows
    ]
    next_offset = offset + len(items)
    return {
        "job_id": job["id"],
        "state": job["state"],
        "client_ref": job["client_ref"],
        "total": job["total"],
        "progress": progress,
        "paused_reason": job["paused_reason"],
        "created_at": job["created_at"],
        "completed_at": job["completed_at"],
        "items": items,
        "next_offset": next_offset if next_offset < job["total"] else None,
    }


async def cancel(job_id: str) -> Optional[dict[str, Any]]:
    """Cancel pending items, keep produced results. Idempotent on terminal jobs."""
    job = await _to_thread(store.get_job, job_id)
    if job is None:
        return None
    if await _to_thread(store.cancel_job, job_id):
        logger.info("batch cancelled job_id=%s", job_id)
        await on_job_terminal(job_id)
    job = await _to_thread(store.get_job, job_id)
    return {
        "job_id": job_id,
        "state": job["state"],
        "total": job["total"],
        "progress": await _to_thread(store.job_progress, job_id),
    }


async def iter_all_items(job_id: str, page: int = 500):
    """All items of a job in index order, for the CSV export."""
    offset = 0
    while True:
        rows = await _to_thread(store.list_items, job_id, offset=offset, limit=page)
        if not rows:
            return
        for row in rows:
            yield row
        offset += len(rows)


async def on_job_terminal(job_id: str) -> None:
    """A job reached completed or cancelled: send its webhook, if it has one."""
    job = await _to_thread(store.get_job, job_id)
    if job is not None and job["callback_url"]:
        webhook.deliver_background(job_id)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class BatchWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._stopping = False
        self._last_purge = 0.0
        self.reset()

    def reset(self) -> None:
        """Clear in-memory breaker state (startup and tests)."""
        self.blocked_streak = 0
        self.paused_since: Optional[str] = None
        self.paused_reason: Optional[str] = None
        self.probe_delay = PROBE_BASE_SECONDS
        self.next_probe_at = 0.0
        self.scrapes = 0
        self.items_processed = 0
        self._last_pause_alert_at: Optional[float] = None
        self._pause_alert_sent = False

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Recover from a restart and launch the worker task."""
        self.reset()
        items, jobs = await _to_thread(store.recover)
        if items or jobs:
            logger.warning(
                "batch recovery: %d in-flight items back to pending, %d paused jobs resumed",
                items,
                jobs,
            )
        self._wake = asyncio.Event()
        self._stopping = False
        self._last_purge = 0.0
        self._task = asyncio.create_task(self._run(), name="batch-worker")
        logger.info("batch worker started")

    async def stop(self, timeout: float = 5.0) -> None:
        """Let the current step finish (a restart loses at most the scrape in
        flight either way), cancel after `timeout`, and drain DB threads."""
        if self._task is None:
            return
        self._stopping = True
        self.wake()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await drain_threads()
        self._task = None
        logger.info("batch worker stopped")

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("batch worker step failed")
                delay = 5.0
                # The single worker is between steps, so anything still in
                # flight was orphaned by this failure; make it pending again.
                try:
                    released = await _to_thread(store.release_in_flight)
                    if released:
                        logger.warning("batch released %d orphaned in-flight items", released)
                except Exception:
                    logger.exception("batch could not release in-flight items")
            if delay > 0 and self._wake is not None and not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    # ---- one unit of work -------------------------------------------------

    async def step(self) -> float:
        """Resolve at most one address. Returns seconds to idle (0 = go again)."""
        if self._wake is not None:
            self._wake.clear()
        await self._maybe_purge()

        if self.paused:
            return await self._paused_step()

        item = await _to_thread(store.next_due_item, store.WORKABLE_JOB_STATES)
        if item is None:
            await self._complete(await _to_thread(store.active_job_ids))
            return await self._idle_seconds(store.WORKABLE_JOB_STATES)

        await self._process(item, store.WORKABLE_JOB_STATES)
        return 0.0

    async def _idle_seconds(self, states: tuple[str, ...]) -> float:
        next_at = await _to_thread(store.next_attempt_at, states)
        if not next_at:
            return IDLE_SECONDS
        try:
            due = datetime.fromisoformat(next_at)
        except ValueError:
            return IDLE_SECONDS
        seconds = (due - datetime.now(timezone.utc)).total_seconds()
        return min(max(seconds, 0.05), IDLE_SECONDS)

    async def _process(
        self, item: dict[str, Any], states: tuple[str, ...], *, probe: bool = False
    ) -> Optional[FailureType]:
        claimed = await _to_thread(store.claim_key, item["address_key"], states)
        if not claimed:
            return None

        try:
            outcome = await lookup_by_address(item["address_raw"], batch=True)
        except Exception as exc:
            # Never leave claimed items in flight: count it as an `error`
            # attempt so the item retries with backoff like any other error.
            logger.exception(
                "batch lookup raised job_id=%s idx=%s", item["job_id"], item["idx"]
            )
            outcome = LookupOutcome.failed(FailureType.ERROR, f"{type(exc).__name__}: {exc}")
        self.items_processed += 1
        if outcome.source == "scrape":
            self.scrapes += 1
        failure = None if outcome.ok else (outcome.failure or FailureType.ERROR)

        updates = [self._item_update(row, outcome, failure, probe) for row in claimed]
        await _to_thread(store.update_items, updates)
        for row, update in zip(claimed, updates):
            logger.info(
                "batch item job_id=%s idx=%s address=%r state=%s source=%s "
                "error_type=%s attempts=%s probe=%s",
                row["job_id"],
                row["idx"],
                item["address_raw"],
                update["state"],
                update.get("source"),
                update.get("error_type"),
                update["attempts"],
                probe,
            )

        await self._update_breaker(failure, probe)
        await self._complete(row["job_id"] for row in claimed)
        return failure

    @staticmethod
    def _item_update(
        row: dict[str, Any],
        outcome: LookupOutcome,
        failure: Optional[FailureType],
        probe: bool,
    ) -> dict[str, Any]:
        base = {"job_id": row["job_id"], "idx": row["idx"]}
        if failure is None or failure is FailureType.NOT_FOUND:
            return {**base, **_terminal_fields(outcome), "attempts": row["attempts"] + 1}

        if probe and failure is FailureType.BLOCKED:
            # A probe that is still blocked must not burn the item's attempts.
            return {
                **base,
                "state": store.PENDING,
                "attempts": row["attempts"],
                "error_type": failure.value,
                "source": outcome.source,
            }

        attempts = row["attempts"] + 1
        if failure in _RETRYABLE and attempts < settings.batch_item_max_attempts:
            return {
                **base,
                "state": store.PENDING,
                "attempts": attempts,
                "error_type": failure.value,
                "source": outcome.source,
                "next_attempt_at": _future_iso(RETRY_BASE_SECONDS * 2 ** (attempts - 1)),
            }
        # Terminal. The failure is already in failed_lookups: lookup_by_address
        # records every scrape failure there, so /failures stays complete.
        return {
            **base,
            "state": store.FAILED,
            "attempts": attempts,
            "error_type": failure.value,
            "source": outcome.source,
        }

    async def _complete(self, job_ids) -> None:
        for job_id in await _to_thread(store.complete_finished_jobs, list(job_ids)):
            logger.info("batch completed job_id=%s", job_id)
            await on_job_terminal(job_id)

    # ---- circuit breaker --------------------------------------------------

    async def _update_breaker(self, failure: Optional[FailureType], probe: bool) -> None:
        # A page that answered (a result, or a genuine not-found) proves BC is
        # serving us again; timeouts and errors prove nothing either way.
        healthy = failure is None or failure is FailureType.NOT_FOUND
        if failure is FailureType.BLOCKED:
            self.blocked_streak += 1
        else:
            self.blocked_streak = 0

        if probe and self.paused:
            if healthy:
                await self._resume("probe lookup succeeded")
            else:
                self.probe_delay = min(self.probe_delay * 2, PROBE_MAX_SECONDS)
                self.next_probe_at = time.monotonic() + self.probe_delay
                logger.warning(
                    "batch probe failed (%s); next probe in %.0fs",
                    failure.value if failure else "unknown",
                    self.probe_delay,
                )
            return

        if self.blocked_streak >= max(settings.batch_pause_after_blocked, 1):
            await self._pause(f"blocked: {self.blocked_streak} consecutive blocked results")

    async def _pause(self, reason: str) -> None:
        self.paused_since = store.now_iso()
        self.paused_reason = reason
        self.probe_delay = PROBE_BASE_SECONDS
        self.next_probe_at = time.monotonic() + self.probe_delay
        ids = await _to_thread(
            store.set_job_states, store.WORKABLE_JOB_STATES, store.PAUSED, paused_reason=reason
        )
        logger.warning("batch paused jobs=%s reason=%s", ids, reason)

        # De-duplicated: a pause/resume flap inside ALERT_MIN_INTERVAL_SECONDS
        # sends one message, not one per pause.
        now = time.monotonic()
        last = self._last_pause_alert_at
        if last is not None and now - last < settings.alert_min_interval_seconds:
            self._pause_alert_sent = False
            return
        self._last_pause_alert_at = now
        self._pause_alert_sent = True
        send_telegram_message_background(
            "BC Assessment: batch PAUSED\n"
            f"Reason: {reason}\n"
            f"Jobs paused: {', '.join(ids) or 'none'}\n"
            f"Timestamp: {self.paused_since}\n"
            f"The worker will probe one item every {PROBE_BASE_SECONDS:.0f}s "
            f"(doubling, max {PROBE_MAX_SECONDS:.0f}s) and resume when it succeeds.\n"
            "Suggested action: check /health and the saved HTML snapshots; "
            "live /address traffic is unaffected by the pause."
        )

    async def _resume(self, reason: str) -> None:
        self.paused_since = None
        self.paused_reason = None
        self.blocked_streak = 0
        self.probe_delay = PROBE_BASE_SECONDS
        ids = await _to_thread(store.set_job_states, (store.PAUSED,), store.RUNNING)
        logger.warning("batch resumed jobs=%s reason=%s", ids, reason)
        if self._pause_alert_sent:
            self._pause_alert_sent = False
            send_telegram_message_background(
                "BC Assessment: batch resumed\n"
                f"Reason: {reason}\n"
                f"Jobs resumed: {', '.join(ids) or 'none'}\n"
                f"Timestamp: {store.now_iso()}"
            )
        self.wake()

    async def _paused_step(self) -> float:
        # A live /address success since the pause also proves BC is reachable.
        health = browser_manager.health
        if (
            health.last_success_at
            and self.paused_since
            and health.last_success_at > self.paused_since
            and health.consecutive_failures == 0
        ):
            await self._resume("scraper healthy again")
            return 0.0

        wait = self.next_probe_at - time.monotonic()
        if wait > 0:
            return min(wait, IDLE_SECONDS)

        item = await _to_thread(store.next_due_item, (store.PAUSED,))
        if item is None:
            # Nothing paused is due (all cancelled or backing off): stop pausing.
            await self._resume("no paused work left to probe")
            return 0.0

        self.next_probe_at = time.monotonic() + self.probe_delay
        logger.info("batch probing job_id=%s idx=%s", item["job_id"], item["idx"])
        await self._process(item, (store.PAUSED,), probe=True)
        return 0.0

    # ---- retention --------------------------------------------------------

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if self._last_purge and now - self._last_purge < PURGE_EVERY_SECONDS:
            return
        self._last_purge = now
        await purge_expired()

    def stats(self) -> dict[str, Any]:
        return {
            "worker_running": self.running,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "blocked_streak": self.blocked_streak,
            "items_processed": self.items_processed,
            "scrapes": self.scrapes,
        }


async def purge_expired() -> int:
    """Delete finished jobs older than BATCH_RETENTION_DAYS (B17)."""
    days = settings.batch_retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    removed = await _to_thread(store.purge, cutoff)
    if removed:
        logger.info("batch purge removed %d jobs older than %d days", removed, days)
    return removed


batch_worker = BatchWorker()


def result_fields() -> list[str]:
    """Column order for the CSV export: the /address result fields."""
    return list(AssessmentResult.model_fields)
