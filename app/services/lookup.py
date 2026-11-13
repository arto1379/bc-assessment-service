"""Lookup orchestration: cache -> negative cache -> scrape -> classify -> record.

Ordering note (this was the hang): the rate limiter is acquired *before* the
scrape lock. Previously `bc_rate_limiter.acquire()` ran inside the lock, so
hitting the hourly cap made one request sleep for up to an hour while holding
the semaphore, blocking every other request behind it.

The rate-limit wait itself is now bounded too: a live request whose projected
wait exceeds `SCRAPE_MAX_PROJECTED_WAIT_SECONDS` gets `busy` with a
`retry_after` hint instead of sleeping.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from typing import Optional

from app.config import settings
from app.db import failures
from app.db.cache import cache_get, cache_set
from app.schemas import AssessmentResult, FailureType, LookupOutcome
from app.services.browser import browser_manager
from app.services.rate_limit import bc_rate_limiter
from app.services.scrape_gate import ScrapeGate
from app.services.scraper import BC_URL, scrape_address
from app.services.stats import (
    is_valid_assessment,
    notify_failure,
    notify_no_address,
    notify_not_found,
    notify_recovered,
    record_bc_scrape,
    record_cache_hit,
)

logger = logging.getLogger(__name__)

# Single-flight scrape slot; live requests are served before batch items.
_scrape_gate = ScrapeGate()
_queue_depth = 0
_queue_lock = asyncio.Lock()


def _normalize_address(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().upper())


# ---------------------------------------------------------------------------
# Mock provider (LOOKUP_PROVIDER=mock) — lets tests and local dev run the whole
# request path without Playwright or any network access.
# ---------------------------------------------------------------------------


def _mock_lookup(address: str) -> LookupOutcome:
    upper = address.upper()
    if any(token in upper for token in ("NOWHERE", "INVALID", "DOESNOTEXIST")):
        return LookupOutcome.failed(
            FailureType.NOT_FOUND, "mock provider: unknown address", url=BC_URL
        )
    if "BLOCKED" in upper:
        return LookupOutcome.failed(
            FailureType.BLOCKED, "mock provider: simulated block", url=BC_URL
        )

    # Derive a stable, address-specific value so tests can assert that two
    # different addresses never return the same figures.
    digest = int(hashlib.sha1(upper.encode()).hexdigest()[:6], 16)
    return LookupOutcome.success(
        AssessmentResult(
            total_value=f"${500_000 + digest % 1_500_000:,}",
            year_built=str(1950 + digest % 70),
            number_of_bed=str(1 + digest % 5),
            number_of_baths=str(1 + digest % 4),
            description=None,
            land_size=None,
            strata_area=str(600 + digest % 2000),
            building_storeys=str(1 + digest % 30),
        )
    )


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


async def _scrape_once(address: str) -> LookupOutcome:
    async with browser_manager.lookup_context() as (page, restarted):
        return await scrape_address(page, address, restarted=restarted)


async def _scrape_with_ceiling(address: str) -> LookupOutcome:
    """One scrape under the hard total timeout. Caller holds the scrape slot."""
    try:
        return await asyncio.wait_for(
            _scrape_once(address),
            timeout=settings.scrape_total_timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        # A lookup that blows the overall budget means the browser is
        # in a bad state; force a relaunch before the next request.
        browser_manager.request_restart("lookup exceeded total timeout")
        return LookupOutcome.failed(
            FailureType.TIMEOUT,
            f"lookup exceeded {settings.scrape_total_timeout_ms}ms overall budget",
            step="total_timeout",
        )
    except Exception as exc:
        logger.exception("Unexpected scrape failure for %r", address)
        browser_manager.request_restart(f"unexpected error: {type(exc).__name__}")
        return LookupOutcome.failed(FailureType.ERROR, f"{type(exc).__name__}: {exc}")


def _busy(reason: str, wait_seconds: float) -> LookupOutcome:
    return LookupOutcome.failed(
        FailureType.BUSY, reason, retry_after=max(1, math.ceil(wait_seconds))
    )


async def _run_batch_scrape(address: str) -> LookupOutcome:
    """Batch path: wait (never shed), yield to live traffic, never sleep holding
    the slot. Sleeps happen only in this loop, outside the gate."""
    while True:
        wait = bc_rate_limiter.projected_wait(batch=True)
        if wait > 0 or _scrape_gate.live_pending:
            await asyncio.sleep(max(wait, 0.05))
            continue
        async with _scrape_gate.slot(batch=True):
            # A live request may have taken the token while we waited for the
            # slot; if so, release it and go back to sleeping outside the gate.
            if bc_rate_limiter.try_acquire(batch=True) == 0:
                return await _scrape_with_ceiling(address)


async def _run_scrape(address: str, *, batch: bool = False) -> LookupOutcome:
    """Rate-limit, then scrape under a hard timeout and a single-flight lock."""
    global _queue_depth

    if batch:
        return await _run_batch_scrape(address)

    max_depth = settings.scrape_max_queue_depth
    async with _queue_lock:
        if max_depth > 0 and _queue_depth >= max_depth:
            logger.warning(
                "Rejecting lookup for %r: %d requests already queued",
                address,
                _queue_depth,
            )
            return _busy(
                f"{_queue_depth} lookups already queued; try again shortly",
                max(
                    bc_rate_limiter.projected_wait(),
                    settings.bc_rate_limit_min_interval_seconds,
                ),
            )
        _queue_depth += 1

    try:
        # Registering live demand first makes queued batch work yield both the
        # rate-limit token and the scrape slot to this request.
        async with _scrape_gate.live_demand():
            # Outside the scrape slot on purpose — see the module docstring.
            # With a max_wait the limiter returns the projected wait instead of
            # sleeping past it, so this request never hangs at the hourly cap.
            max_wait = settings.scrape_max_projected_wait_seconds
            wait = await bc_rate_limiter.acquire(max_wait=max_wait)
            if wait is not None:
                logger.warning(
                    "Rejecting lookup for %r: projected rate-limit wait %.0fs > %.0fs",
                    address,
                    wait,
                    max_wait,
                )
                return _busy(f"rate limit reached; projected wait {wait:.0f}s", wait)

            async with _scrape_gate.slot():
                return await _scrape_with_ceiling(address)
    finally:
        async with _queue_lock:
            _queue_depth -= 1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def lookup_by_address(address: str) -> LookupOutcome:
    """Return assessment data for an address, or a classified failure."""
    if not address or not address.strip():
        logger.warning("lookup_by_address: empty address")
        notify_no_address(address or "")
        return LookupOutcome.failed(FailureType.NOT_FOUND, "empty address")

    address = address.strip()
    normalized = _normalize_address(address)
    logger.info("lookup address=%r normalized=%r", address, normalized)

    # 1. Positive cache.
    cached = await asyncio.to_thread(cache_get, normalized)
    if cached is not None and is_valid_assessment(cached):
        logger.info("cache hit address=%r", normalized)
        await record_cache_hit(address)
        return LookupOutcome.success(cached)

    # 2. Negative cache — a recent, genuine "not found".
    negative = await asyncio.to_thread(failures.get_negative_cache, normalized)
    if negative is not None:
        logger.info("negative cache hit address=%r (%s)", normalized, negative)
        await record_cache_hit(address)
        return LookupOutcome.failed(FailureType.NOT_FOUND, negative)

    # 3. Scrape. Counted before dispatch so the counter reflects every call that
    # reached the provider, which is what the cache is meant to avoid.
    await record_bc_scrape(address)
    if settings.lookup_provider.lower() == "mock":
        outcome = _mock_lookup(address)
    else:
        outcome = await _run_scrape(address)

    await _record_outcome(address, normalized, outcome)
    return outcome


async def _record_outcome(
    address: str, normalized: str, outcome: LookupOutcome
) -> None:
    """Persist and alert on the result of a scrape."""
    if outcome.ok and outcome.result is not None:
        browser_manager.record_success()
        await asyncio.to_thread(cache_set, normalized, outcome.result)
        await asyncio.to_thread(failures.mark_resolved, normalized)
        await notify_recovered(address)
        return

    failure = outcome.failure or FailureType.ERROR

    # BUSY is a load-shedding signal, not a scraper fault: it says nothing about
    # browser health and should not be queued for retry.
    if failure is FailureType.BUSY:
        logger.warning("lookup shed load for address=%r", address)
        return

    browser_manager.record_failure(failure)

    await asyncio.to_thread(
        failures.record_failure,
        address,
        normalized,
        failure,
        reason=outcome.reason,
        step=outcome.step,
        url=outcome.url,
        snapshot_path=outcome.snapshot_path,
    )

    logger.warning(
        "lookup failed address=%r type=%s step=%s url=%s reason=%s restarted=%s",
        address,
        failure.value,
        outcome.step,
        outcome.url,
        outcome.reason,
        outcome.restarted,
    )

    if failure is FailureType.NOT_FOUND:
        notify_not_found(address)
    else:
        await notify_failure(address, outcome)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def warmup_browser() -> None:
    """Pre-load the browser so the first API request is not slowed by launch."""
    if not settings.scrape_warmup_on_start:
        return
    if settings.lookup_provider.lower() == "mock":
        return
    try:
        await browser_manager.ensure_browser()
        logger.info("BC Assessment browser warmed up")
    except Exception:
        logger.exception("Browser warmup failed; will retry on first request")


async def shutdown_browser() -> None:
    await browser_manager.shutdown()


async def retry_failed_lookups(limit: int = 10) -> dict:
    """Re-attempt queued failures whose backoff has elapsed."""
    due = await asyncio.to_thread(
        failures.list_failures, limit=limit, due_only=True
    )
    retried, recovered = 0, 0
    for row in due:
        if row["failure_type"] == FailureType.NOT_FOUND.value:
            continue
        retried += 1
        outcome = await lookup_by_address(row["address"])
        if outcome.ok:
            recovered += 1
    return {"due": len(due), "retried": retried, "recovered": recovered}
