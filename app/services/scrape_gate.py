"""The single scrape slot, with live requests ahead of batch work.

This replaces the plain `asyncio.Semaphore(1)`. Scraping stays single-flight;
the only change is who gets the slot next. A live `/address` request registers
demand (`live_demand()`) as soon as it starts waiting, including while it waits
for a rate-limit slot, and batch work does not take the slot or a rate-limit
token while any live demand exists. A scrape in progress is never interrupted,
so a live request waits behind at most one scrape.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ScrapeGate:
    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._held = False
        self._live_pending = 0

    @property
    def live_pending(self) -> int:
        """Live requests currently waiting for, or holding, the slot."""
        return self._live_pending

    @property
    def held(self) -> bool:
        return self._held

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    @asynccontextmanager
    async def live_demand(self) -> AsyncIterator[None]:
        """Mark a live request as in flight so batch work yields to it."""
        self._live_pending += 1
        try:
            yield
        finally:
            self._live_pending -= 1
            await self._notify()

    @asynccontextmanager
    async def slot(self, *, batch: bool = False) -> AsyncIterator[None]:
        """Hold the single scrape slot. Batch callers also wait for live demand
        to drain, so a waiting live request is always served first."""
        async with self._cond:
            await self._cond.wait_for(
                lambda: not self._held and (not batch or self._live_pending == 0)
            )
            self._held = True
        try:
            yield
        finally:
            self._held = False
            await self._notify()
