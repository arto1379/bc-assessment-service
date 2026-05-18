"""In-memory request counters with Telegram milestone alerts."""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.schemas import AssessmentResult
from app.services.telegram_notify import send_telegram_message_background

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_cache_calls = 0
_bc_calls = 0


def is_valid_assessment(result: AssessmentResult | None) -> bool:
    if result is None:
        return False
    total = result.total_value
    return bool(total and str(total).strip())


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


def notify_no_address(address: str) -> None:
    send_telegram_message_background(
        f"BC Assessment: no address\n"
        f"Received empty or whitespace-only address.\n"
        f"Input: {address!r}"
    )


def notify_not_found(address: str) -> None:
    send_telegram_message_background(
        f"BC Assessment: property not found\n"
        f"Address: {address}"
    )


def notify_invalid_result(address: str, reason: str) -> None:
    send_telegram_message_background(
        f"BC Assessment: invalid result\n"
        f"Address: {address}\n"
        f"Reason: {reason}"
    )


async def get_stats() -> dict[str, int]:
    async with _lock:
        return {"cache_calls": _cache_calls, "bc_calls": _bc_calls}
