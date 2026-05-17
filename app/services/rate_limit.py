"""Rate limiters for bcassessment.ca requests (window caps + minimum spacing)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class _RateLimiter(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def acquire(self) -> None: ...


class BCRateLimiter:
    """Allow at most `max_calls` BC site operations per `window_seconds`."""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        *,
        label: str = "BC",
    ) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.label = label
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_calls > 0 and self.window_seconds > 0

    async def acquire(self) -> None:
        if not self.enabled:
            return

        async with self._lock:
            while True:
                now = time.monotonic()
                while (
                    self._timestamps
                    and now - self._timestamps[0] >= self.window_seconds
                ):
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return

                wait_seconds = self.window_seconds - (now - self._timestamps[0])
                logger.info(
                    "%s rate limit reached (%s per %ss); waiting %.1fs",
                    self.label,
                    self.max_calls,
                    self.window_seconds,
                    wait_seconds,
                )
                await asyncio.sleep(max(wait_seconds, 0.05))


class BCMinIntervalLimiter:
    """Enforce a minimum delay between consecutive BC site operations."""

    def __init__(self, min_interval_seconds: float, *, label: str = "BC spacing") -> None:
        self.min_interval_seconds = min_interval_seconds
        self.label = label
        self._last_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.min_interval_seconds > 0

    async def acquire(self) -> None:
        if not self.enabled:
            return

        async with self._lock:
            now = time.monotonic()
            if self._last_at is not None:
                wait_seconds = self.min_interval_seconds - (now - self._last_at)
                if wait_seconds > 0:
                    logger.info(
                        "%s: waiting %.1fs (min %.1fs between requests)",
                        self.label,
                        wait_seconds,
                        self.min_interval_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
            self._last_at = time.monotonic()


class BCRateLimitGroup:
    """Apply multiple rate-limit tiers (all must pass before proceeding)."""

    def __init__(self, *limiters: _RateLimiter) -> None:
        self._limiters = [lim for lim in limiters if lim.enabled]

    async def acquire(self) -> None:
        for limiter in self._limiters:
            await limiter.acquire()


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
)
