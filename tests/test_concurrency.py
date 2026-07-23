"""Coverage for the hang fix itself: the queue guard, the total-timeout ceiling
and the rate limiter.

`lookup_by_address` branches to `_mock_lookup` before ever reaching
`_run_scrape`, so the mock provider — and therefore `scripts/load_test.py` run
against a mock server — exercises none of this. These tests drive `_run_scrape`
directly with a stubbed scrape so the production concurrency path is covered
without Playwright.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import settings
from app.schemas import AssessmentResult, FailureType, LookupOutcome
from app.services import lookup as lookup_mod
from app.services.rate_limit import BCRateLimiter


class NullLimiter:
    """Stands in for `bc_rate_limiter` so these tests never really sleep."""

    async def acquire(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    """`_scrape_lock` is built at import time and binds to the first event loop
    that awaits it, so each test needs its own."""
    monkeypatch.setattr(lookup_mod, "_scrape_lock", asyncio.Semaphore(1))
    monkeypatch.setattr(lookup_mod, "_queue_lock", asyncio.Lock())
    monkeypatch.setattr(lookup_mod, "_queue_depth", 0)
    monkeypatch.setattr(lookup_mod, "bc_rate_limiter", NullLimiter())


def _ok() -> LookupOutcome:
    return LookupOutcome.success(AssessmentResult(total_value="$1"))


class TestQueueGuard:
    async def test_requests_beyond_the_queue_depth_are_shed(self, monkeypatch):
        """Load shedding is what stops callers piling up behind a slow scrape."""
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 3)
        release = asyncio.Event()

        async def slow_scrape(address):
            await release.wait()
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", slow_scrape)

        queued = [
            asyncio.create_task(lookup_mod._run_scrape(f"A{i}")) for i in range(3)
        ]
        await asyncio.sleep(0.05)
        shed = await lookup_mod._run_scrape("OVERFLOW")

        release.set()
        await asyncio.gather(*queued)

        assert shed.failure is FailureType.BUSY

    async def test_shed_requests_are_not_queued_for_retry(self, monkeypatch):
        """BUSY is load shedding, not a scraper fault — it says nothing about
        browser health and must not enter the retry queue."""
        from app.db import failures

        monkeypatch.setattr(settings, "scrape_max_queue_depth", 1)
        release = asyncio.Event()

        async def slow_scrape(address):
            await release.wait()
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", slow_scrape)

        holder = asyncio.create_task(lookup_mod._run_scrape("HOLDER"))
        await asyncio.sleep(0.05)
        shed = await lookup_mod._run_scrape("SHED")
        await lookup_mod._record_outcome("SHED", "SHED", shed)

        release.set()
        await holder

        assert shed.failure is FailureType.BUSY
        assert failures.list_failures() == []

    async def test_depth_is_released_so_the_guard_does_not_leak(self, monkeypatch):
        """A leaked counter would permanently wedge the service in BUSY."""
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 2)

        async def immediate(address):
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", immediate)

        for _ in range(10):
            assert (await lookup_mod._run_scrape("X")).ok

        assert lookup_mod._queue_depth == 0


class TestTotalTimeout:
    async def test_a_stuck_scrape_fails_fast_and_forces_a_relaunch(self, monkeypatch):
        monkeypatch.setattr(settings, "scrape_total_timeout_ms", 100)
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 20)

        async def hangs(address):
            await asyncio.sleep(30)
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", hangs)

        started = time.monotonic()
        outcome = await lookup_mod._run_scrape("HANGS")
        elapsed = time.monotonic() - started

        assert outcome.failure is FailureType.TIMEOUT
        assert outcome.step == "total_timeout"
        assert elapsed < 2, f"total timeout did not fire promptly ({elapsed:.2f}s)"
        assert lookup_mod.browser_manager._restart_pending is True

    async def test_one_stuck_request_does_not_block_later_requests(self, monkeypatch):
        """The PRD requirement in one test: 'Preventing one stuck request from
        blocking all later requests.'"""
        monkeypatch.setattr(settings, "scrape_total_timeout_ms", 200)
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 20)

        calls = {"n": 0}

        async def first_one_hangs(address):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(30)
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", first_one_hangs)

        stuck = asyncio.create_task(lookup_mod._run_scrape("STUCK"))
        await asyncio.sleep(0.02)
        later = asyncio.create_task(lookup_mod._run_scrape("LATER"))

        stuck_result, later_result = await asyncio.wait_for(
            asyncio.gather(stuck, later), timeout=10
        )

        assert stuck_result.failure is FailureType.TIMEOUT
        assert later_result.ok, "the request behind a stuck one must still be served"

    async def test_an_unexpected_error_is_classified_not_raised(self, monkeypatch):
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 20)

        async def explodes(address):
            raise RuntimeError("chromium went away")

        monkeypatch.setattr(lookup_mod, "_scrape_once", explodes)

        outcome = await lookup_mod._run_scrape("BOOM")

        assert outcome.failure is FailureType.ERROR
        assert "chromium went away" in (outcome.reason or "")
        assert lookup_mod.browser_manager._restart_pending is True


class TestRateLimiter:
    async def test_window_cap_is_enforced(self):
        limiter = BCRateLimiter(max_calls=2, window_seconds=0.4, label="test")

        started = time.monotonic()
        for _ in range(3):
            await limiter.acquire()

        assert time.monotonic() - started >= 0.35, "third call ignored the window cap"

    async def test_a_capped_out_wait_still_serialises_callers(self):
        """Documents real behaviour that the module docstring understates.

        Moving `bc_rate_limiter.acquire()` outside `_scrape_lock` did not make
        waiters independent: `BCRateLimiter.acquire()` sleeps *inside* its own
        `asyncio.Lock`, so callers still queue up — on the limiter's lock rather
        than the scrape lock. What actually prevents a wedge is the queue guard
        and the total-timeout ceiling above, not this reordering.

        Note also that `acquire()` sits outside `asyncio.wait_for` in
        `_run_scrape`, so `SCRAPE_TOTAL_TIMEOUT_MS` does not bound this wait.
        """
        limiter = BCRateLimiter(max_calls=1, window_seconds=0.4, label="test")
        await limiter.acquire()  # consume the only slot

        started = time.monotonic()

        async def waiter() -> float:
            await limiter.acquire()
            return time.monotonic() - started

        first, second = await asyncio.gather(waiter(), waiter())

        assert second - first >= 0.25, (
            "waiters proceeded together; if that is now true, the limiter no "
            "longer sleeps under its own lock and this test should be revisited"
        )
