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

    async def acquire(self, **_kwargs) -> None:
        return None

    def projected_wait(self, **_kwargs) -> float:
        return 0.0

    def try_acquire(self, **_kwargs) -> float:
        return 0.0


class FakeClock:
    """Deterministic monotonic clock for rate-limiter tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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

    async def test_waiters_do_not_hold_a_lock_while_sleeping(self):
        """The old limiter slept inside its own asyncio.Lock, so one capped-out
        waiter blocked everyone behind it, outside SCRAPE_TOTAL_TIMEOUT_MS. Now a
        sleeping waiter blocks nobody: `projected_wait` answers instantly while
        another caller is asleep."""
        limiter = BCRateLimiter(max_calls=1, window_seconds=0.5, label="test")
        await limiter.acquire()  # consume the only slot

        sleeper = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0.05)  # sleeper is now asleep waiting for the window

        started = time.monotonic()
        wait = limiter.projected_wait(time.monotonic())
        assert time.monotonic() - started < 0.01
        assert 0 < wait <= 0.5
        assert not sleeper.done()
        await sleeper


def _group(clock, *, spacing=4.0, short=(15, 60.0), hourly=(500, 3600.0), share=0.7):
    from app.services.rate_limit import BCMinIntervalLimiter, BCRateLimitGroup

    return BCRateLimitGroup(
        BCMinIntervalLimiter(spacing, clock=clock),
        BCRateLimiter(*short, label="short", clock=clock),
        BCRateLimiter(*hourly, label="hourly", clock=clock),
        batch_limiters=(
            BCRateLimiter(int(hourly[0] * share), hourly[1], label="batch", clock=clock),
        ),
        clock=clock,
    )


class TestProjectedWait:
    def test_projected_wait_reports_without_sleeping_or_recording(self):
        clock = FakeClock()
        group = _group(clock)

        assert group.projected_wait() == 0
        assert group.try_acquire() == 0
        # Spacing now applies; asking twice must not consume anything.
        assert group.projected_wait() == pytest.approx(4.0)
        assert group.projected_wait() == pytest.approx(4.0)
        clock.advance(4.0)
        assert group.projected_wait() == 0

    def test_at_the_hourly_cap_the_wait_is_reported_not_slept(self):
        clock = FakeClock()
        group = _group(clock, spacing=0, short=(0, 0), hourly=(3, 3600.0))
        for _ in range(3):
            assert group.try_acquire() == 0

        assert group.try_acquire() == pytest.approx(3600.0)

    async def test_acquire_with_max_wait_returns_promptly_instead_of_hanging(self):
        """Acceptance: at the hourly cap, /address answers busy within 1 s."""
        from app.services.rate_limit import BCRateLimitGroup

        group = BCRateLimitGroup(BCRateLimiter(1, 3600.0, label="hourly"))
        assert await group.acquire(max_wait=30) is None  # takes the only slot

        started = time.monotonic()
        wait = await group.acquire(max_wait=30)
        assert time.monotonic() - started < 1
        assert wait is not None and wait > 30

    async def test_acquire_waits_when_the_projected_wait_is_within_budget(self):
        from app.services.rate_limit import BCMinIntervalLimiter, BCRateLimitGroup

        group = BCRateLimitGroup(BCMinIntervalLimiter(0.2))
        assert await group.acquire(max_wait=1) is None
        started = time.monotonic()
        assert await group.acquire(max_wait=1) is None
        assert time.monotonic() - started >= 0.15

    def test_batch_never_exceeds_its_hourly_share(self):
        """An hour of back-to-back batch demand on a fake clock."""
        clock = FakeClock()
        group = _group(clock)
        granted = 0
        hour_end = clock.now + 3600
        while clock.now < hour_end:
            wait = group.try_acquire(batch=True)
            if wait == 0:
                granted += 1
            else:
                clock.advance(wait)

        assert granted == 350

    def test_live_traffic_keeps_the_rest_of_the_hourly_cap(self):
        clock = FakeClock()
        group = _group(clock, spacing=0, short=(0, 0))
        for _ in range(350):
            assert group.try_acquire(batch=True) == 0

        assert group.try_acquire(batch=True) > 0, "batch share exhausted"
        live = sum(1 for _ in range(200) if group.try_acquire() == 0)
        assert live == 150, "live requests get the remaining 30% and no more"


class TestBusyRetryAfter:
    async def test_run_scrape_returns_busy_with_retry_after_over_threshold(
        self, monkeypatch
    ):
        from app.services.rate_limit import BCRateLimitGroup

        monkeypatch.setattr(settings, "scrape_max_projected_wait_seconds", 30)
        limiter = BCRateLimitGroup(BCRateLimiter(1, 3600.0, label="hourly"))
        limiter.try_acquire()
        monkeypatch.setattr(lookup_mod, "bc_rate_limiter", limiter)

        async def never_called(address):
            raise AssertionError("must not scrape when the wait is too long")

        monkeypatch.setattr(lookup_mod, "_scrape_once", never_called)

        started = time.monotonic()
        outcome = await lookup_mod._run_scrape("CAPPED")

        assert time.monotonic() - started < 1
        assert outcome.failure is FailureType.BUSY
        assert outcome.retry_after is not None and outcome.retry_after > 30
        assert lookup_mod._queue_depth == 0

    async def test_queue_depth_busy_also_carries_retry_after(self, monkeypatch):
        monkeypatch.setattr(settings, "scrape_max_queue_depth", 1)
        release = asyncio.Event()

        async def slow_scrape(address):
            await release.wait()
            return _ok()

        monkeypatch.setattr(lookup_mod, "_scrape_once", slow_scrape)
        holder = asyncio.create_task(lookup_mod._run_scrape("HOLDER"))
        await asyncio.sleep(0.05)
        shed = await lookup_mod._run_scrape("SHED")
        release.set()
        await holder

        assert shed.failure is FailureType.BUSY
        assert shed.retry_after and shed.retry_after >= 1

    def test_address_endpoint_sets_retry_after_and_keeps_the_busy_body(
        self, client, monkeypatch
    ):
        import app.main as main_mod

        async def busy(address):
            return LookupOutcome.failed(FailureType.BUSY, "capped", retry_after=42)

        monkeypatch.setattr(main_mod, "lookup_by_address", busy)
        response = client.get("/address", params={"address": "1 ANY ST"})

        assert response.status_code == 200  # unchanged: busy is not an HTTP error
        assert response.headers["Retry-After"] == "42"
        assert response.json() == {
            "status": "error",
            "message": "Service busy, please retry shortly",
            "error_type": "busy",
        }

    def test_success_response_has_no_retry_after(self, client):
        response = client.get("/address", params={"address": "7009 TISDALL ST VANCOUVER"})
        assert "Retry-After" not in response.headers
