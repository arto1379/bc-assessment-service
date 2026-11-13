"""Rate limiters for bcassessment.ca requests (window caps + minimum spacing).

Every limiter can say how long the next call would have to wait
(`projected_wait`) without sleeping, and records a call (`record`) as a separate
step. Nothing here sleeps while holding a lock: the check-and-record in
`BCRateLimitGroup.try_acquire` contains no `await`, so on the asyncio event loop
it is already atomic, and callers sleep in their own loop between attempts.

That is the fix for the old unbounded wait: `acquire()` used to sleep inside an
`asyncio.Lock`, outside `SCRAPE_TOTAL_TIMEOUT_MS`, so at the hourly cap a request
could block for up to an hour. Now `/address` asks for the projected wait first
and answers `busy` with `Retry-After` when it is too long.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import Callable, Optional, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

Clock = Callable[[], float]


class _RateLimiter(Protocol):
    @property
    def enabled(self) -> bool: ...

    def projected_wait(self, now: float) -> float: ...

    def record(self, now: float) -> None: ...


class BCRateLimiter:
    """Allow at most `max_calls` BC site operations per `window_seconds`."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        *,
        label: str = "BC",
        clock: Clock = time.monotonic,
    ) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.label = label
        self._clock = clock
        self._timestamps: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return self.max_calls > 0 and self.window_seconds > 0

    def _prune(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
            self._timestamps.popleft()

    def used(self, now: float | None = None) -> int:
        """Calls recorded in the current window."""
        self._prune(self._clock() if now is None else now)
        return len(self._timestamps)

    def projected_wait(self, now: float) -> float:
        if not self.enabled:
            return 0.0
        self._prune(now)
        if len(self._timestamps) < self.max_calls:
            return 0.0
        return max(self.window_seconds - (now - self._timestamps[0]), 0.0)

    def record(self, now: float) -> None:
        if self.enabled:
            self._timestamps.append(now)

    async def acquire(self) -> None:
        """Standalone use: wait for a slot, then take it. Never holds a lock."""
        while (wait := self.projected_wait(self._clock())) > 0:
            logger.info(
                "%s rate limit reached (%s per %ss); waiting %.1fs",
                self.label,
                self.max_calls,
                self.window_seconds,
                wait,
            )
            await asyncio.sleep(max(wait, 0.05))
        self.record(self._clock())


class BCMinIntervalLimiter:
    """Enforce a minimum delay between consecutive BC site operations."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        label: str = "BC spacing",
        clock: Clock = time.monotonic,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.label = label
        self._clock = clock
        self._last_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self.min_interval_seconds > 0

    def projected_wait(self, now: float) -> float:
        if not self.enabled or self._last_at is None:
            return 0.0
        return max(self.min_interval_seconds - (now - self._last_at), 0.0)

    def record(self, now: float) -> None:
        self._last_at = now

    async def acquire(self) -> None:
        """Standalone use: wait out the spacing, then take the slot."""
        while (wait := self.projected_wait(self._clock())) > 0:
            await asyncio.sleep(wait)
        self.record(self._clock())


class BCRateLimitGroup:
    """Apply multiple rate-limit tiers (all must pass before proceeding).

    `batch_limiters` apply only to batch scrapes, on top of the shared tiers. A
    batch scrape is recorded in both, so batch work is held to its share of the
    hourly cap while live `/address` traffic keeps the rest.
    """

    def __init__(
        self,
        *limiters: _RateLimiter,
        batch_limiters: tuple[_RateLimiter, ...] = (),
        clock: Clock = time.monotonic,
    ) -> None:
        self._limiters = [lim for lim in limiters if lim.enabled]
        self._batch_limiters = [lim for lim in batch_limiters if lim.enabled]
        self._clock = clock

    def _tiers(self, batch: bool) -> list[_RateLimiter]:
        return self._limiters + self._batch_limiters if batch else self._limiters

    def projected_wait(self, *, batch: bool = False) -> float:
        """Seconds until a call could proceed. Never sleeps."""
        now = self._clock()
        return max((lim.projected_wait(now) for lim in self._tiers(batch)), default=0.0)

    def try_acquire(self, *, batch: bool = False) -> float:
        """Take a slot if one is free now and return 0, else return the wait.

        There is no `await` between the check and the record, so this is atomic
        on the event loop and needs no lock.
        """
        now = self._clock()
        tiers = self._tiers(batch)
        wait = max((lim.projected_wait(now) for lim in tiers), default=0.0)
        if wait > 0:
            return wait
        for lim in tiers:
            lim.record(now)
        return 0.0

    async def acquire(
        self, *, batch: bool = False, max_wait: float | None = None
    ) -> Optional[float]:
        """Wait for a slot without holding any lock.

        Returns None once the slot is taken. With `max_wait`, returns the
        projected wait instead of sleeping past `max_wait` seconds in total, so
        the caller can answer `busy` rather than hang.
        """
        deadline = None if max_wait is None else self._clock() + max_wait
        while True:
            wait = self.try_acquire(batch=batch)
            if wait <= 0:
                return None
            if deadline is not None and self._clock() + wait > deadline:
                return wait
            logger.info(
                "BC rate limit: waiting %.1fs (%s)", wait, "batch" if batch else "live"
            )
            await asyncio.sleep(max(wait, 0.05))

    def snapshot(self) -> dict:
        """Current usage per tier, for /stats."""
        now = self._clock()
        tiers: dict[str, dict] = {}
        for lim in self._limiters + self._batch_limiters:
            if isinstance(lim, BCRateLimiter):
                tiers[lim.label] = {
                    "used": lim.used(now),
                    "max": lim.max_calls,
                    "window_seconds": lim.window_seconds,
                }
        return {
            "projected_wait_seconds": round(self.projected_wait(), 2),
            "batch_projected_wait_seconds": round(self.projected_wait(batch=True), 2),
            "tiers": tiers,
        }


def batch_hourly_max() -> int:
    """Batch's share of the hourly cap (350 of 500 by default)."""
    share = min(max(settings.batch_hourly_share, 0.0), 1.0)
    return math.floor(settings.bc_rate_limit_hourly_max * share)


def seconds_per_batch_scrape() -> float:
    """Sustained seconds between batch scrapes, for job estimates."""
    per_hour = batch_hourly_max()
    window = settings.bc_rate_limit_hourly_window_seconds
    sustained = window / per_hour if per_hour > 0 else window
    return max(settings.bc_rate_limit_min_interval_seconds, sustained)


bc_rate_limiter = BCRateLimitGroup(
    BCMinIntervalLimiter(
        settings.bc_rate_limit_min_interval_seconds,
        label="BC spacing",
    ),
    BCRateLimiter(
        settings.bc_rate_limit_max,
        settings.bc_rate_limit_window_seconds,
        label="BC short",
    ),
    BCRateLimiter(
        settings.bc_rate_limit_hourly_max,
        settings.bc_rate_limit_hourly_window_seconds,
        label="BC hourly",
    ),
    batch_limiters=(
        BCRateLimiter(
            batch_hourly_max(),
            settings.bc_rate_limit_hourly_window_seconds,
            label="BC hourly (batch)",
        ),
    ),
)
