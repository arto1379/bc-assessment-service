"""Request counters, failure alerting and de-duplication."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.schemas import AssessmentResult, FailureType, LookupOutcome
from app.services.telegram_notify import send_telegram_message_background

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_cache_calls = 0
_bc_calls = 0

# Last time we alerted about each failure type, so a block storm produces one
# message rather than one per request.
_last_alert_at: dict[str, float] = {}
_alerting_lock = asyncio.Lock()
_in_failure_state = False

_SUGGESTED_ACTION = {
    FailureType.BLOCKED: (
        "Check the saved HTML snapshot, then pause traffic and raise "
        "BC_RATE_LIMIT_MIN_INTERVAL_SECONDS before resuming."
    ),
    FailureType.TIMEOUT: (
        "Check VPS network/CPU and whether bcassessment.ca is slow or changed "
        "its markup."
    ),
    FailureType.PARSE_EMPTY: (
        "BC Assessment likely changed its page layout — review the snapshot and "
        "update the selectors in app/services/scraper.py."
    ),
    FailureType.ERROR: "Check journalctl -u bc-assessment for the traceback.",
}


def is_valid_assessment(result: AssessmentResult | None) -> bool:
    if result is None:
        return False
    total = result.total_value
    return bool(total and str(total).strip())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


async def record_cache_hit(address: str = "") -> None:
    global _cache_calls, _bc_calls
    milestone_msg: str | None = None

    async with _lock:
        _cache_calls += 1
        cache_n, bc_n = _cache_calls, _bc_calls
        if cache_n >= settings.stats_milestone_count:
            _cache_calls = 0
            _bc_calls = 0
            milestone_msg = (
                f"BC Assessment stats milestone\n"
                f"Cache lookups reached {settings.stats_milestone_count}.\n"
                f"Counts reset.\n"
                f"Last address: {address or 'n/a'}\n"
                f"At reset — cache: {cache_n}, BC scrape: {bc_n}"
            )

    if milestone_msg:
        logger.info("Stats milestone (cache): resetting counters")
        send_telegram_message_background(milestone_msg)


async def record_bc_scrape(address: str = "") -> None:
    global _cache_calls, _bc_calls
    milestone_msg: str | None = None

    async with _lock:
        _bc_calls += 1
        cache_n, bc_n = _cache_calls, _bc_calls
        if bc_n >= settings.stats_milestone_count:
            _cache_calls = 0
            _bc_calls = 0
            milestone_msg = (
                f"BC Assessment stats milestone\n"
                f"BC Assessment scrapes reached {settings.stats_milestone_count}.\n"
                f"Counts reset.\n"
                f"Last address: {address or 'n/a'}\n"
                f"At reset — cache: {cache_n}, BC scrape: {bc_n}"
            )

    if milestone_msg:
        logger.info("Stats milestone (BC): resetting counters")
        send_telegram_message_background(milestone_msg)


async def get_stats() -> dict[str, int]:
    async with _lock:
        return {"cache_calls": _cache_calls, "bc_calls": _bc_calls}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def notify_no_address(address: str) -> None:
    send_telegram_message_background(
        "BC Assessment: no address\n"
        "Received empty or whitespace-only address.\n"
        f"Input: {address!r}"
    )


def notify_not_found(address: str) -> None:
    send_telegram_message_background(
        f"BC Assessment: property not found\nAddress: {address}"
    )


async def notify_failure(address: str, outcome: LookupOutcome) -> None:
    """Alert on a scraper failure, de-duplicated per failure type.

    Blocking is the case the PRD cares most about, so the message carries every
    field it asks for: address, failure type, URL, reason, timestamp, whether
    the browser was restarted, and a suggested next action.
    """
    global _in_failure_state

    failure = outcome.failure
    if failure is None or failure is FailureType.NOT_FOUND:
        return

    key = failure.value
    interval = settings.alert_min_interval_seconds

    async with _alerting_lock:
        now = time.monotonic()
        last = _last_alert_at.get(key)
        if last is not None and (now - last) < interval:
            logger.info(
                "Suppressing duplicate %s alert (last sent %.0fs ago)",
                key,
                now - last,
            )
            return
        _last_alert_at[key] = now
        _in_failure_state = True

    lines = [
        f"BC Assessment: {failure.value.upper()}",
        f"Address: {address}",
        f"Failure type: {failure.value}",
        f"Failed step: {outcome.step or 'n/a'}",
        f"URL: {outcome.url or 'n/a'}",
        f"Reason: {outcome.reason or 'n/a'}",
        f"Timestamp: {_now_iso()}",
        f"Browser restarted: {'yes' if outcome.restarted else 'no'}",
    ]
    if outcome.snapshot_path:
        lines.append(f"HTML snapshot: {outcome.snapshot_path}")
    action = _SUGGESTED_ACTION.get(failure)
    if action:
        lines.append(f"Suggested action: {action}")

    send_telegram_message_background("\n".join(lines))


async def notify_recovered(address: str) -> None:
    """Send a single all-clear once scraping works again after failures."""
    global _in_failure_state

    async with _alerting_lock:
        if not _in_failure_state:
            return
        _in_failure_state = False
        _last_alert_at.clear()

    send_telegram_message_background(
        "BC Assessment: recovered\n"
        f"Scraping succeeded again at {_now_iso()}.\n"
        f"First successful address: {address}"
    )


def reset_alert_state_for_tests() -> None:
    _last_alert_at.clear()
    global _in_failure_state
    _in_failure_state = False
