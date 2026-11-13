"""Batch worker: retries, backoff, circuit breaker, restart, purge, sharing.

These drive `BatchWorker.step()` through the real batch scrape path
(`lookup_by_address(batch=True)` -> `_run_batch_scrape` -> gate -> limiter) with
only the browser scrape itself stubbed, as `test_concurrency.py` does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import batch as store
from app.db import failures
from app.db.cache import cache_set
from app.db.connection import get_connection
from app.schemas import AssessmentResult, FailureType, LookupOutcome
from app.services import batch as batch_service
from app.services import lookup as lookup_mod
from app.services import stats as stats_mod
from app.services.browser import browser_manager
from app.services.scrape_gate import ScrapeGate


class NullLimiter:
    async def acquire(self, **_kwargs):
        return None

    def projected_wait(self, **_kwargs):
        return 0.0

    def try_acquire(self, **_kwargs):
        return 0.0


class StubScraper:
    """Per-address scripted outcomes; counts every real scrape."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.script: dict[str, list[FailureType | None]] = {}
        self.default: FailureType | None = None

    def set(self, address: str, *outcomes: FailureType | None) -> None:
        self.script[address] = list(outcomes)

    async def __call__(self, address: str) -> LookupOutcome:
        self.calls[address] += 1
        queue = self.script.get(address)
        failure = queue.pop(0) if queue and len(queue) > 1 else (queue[0] if queue else self.default)
        if failure is None:
            return LookupOutcome.success(
                AssessmentResult(total_value=f"${len(address) * 1000 + sum(map(ord, address))}")
            )
        return LookupOutcome.failed(failure, f"stub {failure.value}")


@pytest.fixture
def scraper(monkeypatch):
    monkeypatch.setattr(settings, "lookup_provider", "live")
    monkeypatch.setattr(settings, "batch_enabled", True)
    monkeypatch.setattr(lookup_mod, "_scrape_gate", ScrapeGate())
    monkeypatch.setattr(lookup_mod, "bc_rate_limiter", NullLimiter())
    monkeypatch.setattr(batch_service, "_submit_lock", None)
    stub = StubScraper()
    monkeypatch.setattr(lookup_mod, "_scrape_once", stub)
    return stub


@pytest.fixture
def alerts(monkeypatch):
    """Capture every Telegram message, from the batch layer and per-lookup alerts."""
    sent: list[str] = []
    monkeypatch.setattr(batch_service, "send_telegram_message_background", sent.append)
    monkeypatch.setattr(stats_mod, "send_telegram_message_background", sent.append)
    return sent


@pytest.fixture
def worker(monkeypatch):
    w = batch_service.BatchWorker()
    monkeypatch.setattr(batch_service, "batch_worker", w)
    return w


async def run_until_idle(worker, max_steps=200) -> None:
    for _ in range(max_steps):
        if await worker.step() > 0:
            return
    raise AssertionError("worker never went idle")


def make_all_due() -> None:
    conn = get_connection()
    with conn:
        conn.execute("UPDATE batch_items SET next_attempt_at = NULL")


def items(job_id):
    return store.list_items(job_id, offset=0, limit=1000)


class TestRetries:
    async def test_timeout_retries_with_exponential_backoff_then_fails(
        self, scraper, worker, alerts
    ):
        scraper.set("1 SLOW ST", FailureType.TIMEOUT)
        job = await batch_service.submit(["1 SLOW ST"])

        await worker.step()
        row = get_connection().execute(
            "SELECT state, attempts, next_attempt_at FROM batch_items"
        ).fetchone()
        assert (row["state"], row["attempts"]) == ("pending", 1)
        delay = (
            datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(timezone.utc)
        ).total_seconds()
        assert 25 < delay <= 30, "first retry after ~RETRY_BASE_SECONDS"

        assert await worker.step() > 0, "nothing is due during the backoff"
        assert scraper.calls["1 SLOW ST"] == 1

        make_all_due()
        await worker.step()
        row = get_connection().execute(
            "SELECT attempts, next_attempt_at FROM batch_items"
        ).fetchone()
        delay = (
            datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(timezone.utc)
        ).total_seconds()
        assert row["attempts"] == 2 and 55 < delay <= 60, "backoff doubles"

        make_all_due()
        await worker.step()
        item = items(job["job_id"])[0]
        assert (item["state"], item["error_type"]) == ("failed", "timeout")
        assert store.get_job(job["job_id"])["state"] == "completed"
        assert scraper.calls["1 SLOW ST"] == settings.batch_item_max_attempts

        queued = failures.list_failures()
        assert [f["failure_type"] for f in queued] == ["timeout"], "/failures stays complete"

    async def test_error_recovers_on_retry(self, scraper, worker, alerts):
        scraper.set("2 FLAKY ST", FailureType.ERROR, None)
        job = await batch_service.submit(["2 FLAKY ST"])
        await worker.step()
        make_all_due()
        await run_until_idle(worker)

        item = items(job["job_id"])[0]
        assert item["state"] == "done" and item["error_type"] is None
        assert failures.list_failures() == [], "a later success resolves the failure"

    async def test_not_found_is_final(self, scraper, worker, alerts):
        scraper.set("3 GONE ST", FailureType.NOT_FOUND)
        job = await batch_service.submit(["3 GONE ST"])
        await run_until_idle(worker)

        assert items(job["job_id"])[0]["state"] == "not_found"
        assert scraper.calls["3 GONE ST"] == 1
        assert alerts == [], "not_found is not alerted per batch item"

    async def test_a_lookup_that_raises_never_leaves_items_in_flight(
        self, scraper, worker, alerts, monkeypatch
    ):
        """Regression: an exception between claim and record left the item
        `running` (reported as pending) until the next restart."""

        async def explodes(address, *, batch=False):
            raise IndexError("tuple index out of range")

        monkeypatch.setattr(batch_service, "lookup_by_address", explodes)
        job = await batch_service.submit(["1 RAISE ST"])
        await worker.step()

        row = get_connection().execute(
            "SELECT state, attempts, error_type, next_attempt_at FROM batch_items"
        ).fetchone()
        assert (row["state"], row["attempts"], row["error_type"]) == ("pending", 1, "error")
        assert row["next_attempt_at"], "retried with backoff like any error"

        monkeypatch.setattr(batch_service, "lookup_by_address", lookup_mod.lookup_by_address)
        make_all_due()
        await run_until_idle(worker)
        assert store.get_job(job["job_id"])["state"] == "completed"

    async def test_a_failed_step_releases_orphaned_in_flight_items(self, scraper, worker):
        await batch_service.submit(["1 ORPHAN ST"])
        store.claim_key("1 ORPHAN ST", store.WORKABLE_JOB_STATES)
        assert store.release_in_flight() == 1
        assert store.next_due_item(store.WORKABLE_JOB_STATES)["address_raw"] == "1 ORPHAN ST"

    async def test_parse_empty_fails_without_retry(self, scraper, worker, alerts):
        scraper.set("4 ODD ST", FailureType.PARSE_EMPTY)
        job = await batch_service.submit(["4 ODD ST"])
        await run_until_idle(worker)

        item = items(job["job_id"])[0]
        assert (item["state"], item["error_type"]) == ("failed", "parse_empty")
        assert scraper.calls["4 ODD ST"] == 1


class TestCircuitBreaker:
    async def test_consecutive_blocks_pause_once_and_a_probe_resumes(
        self, scraper, worker, alerts, monkeypatch
    ):
        monkeypatch.setattr(settings, "batch_pause_after_blocked", 3)
        scraper.default = FailureType.BLOCKED
        addresses = [f"{n} BLOCK ST" for n in range(6)]
        job = await batch_service.submit(addresses)

        for _ in range(3):
            await worker.step()

        state = store.get_job(job["job_id"])
        assert state["state"] == "paused"
        assert "3 consecutive blocked" in state["paused_reason"]
        assert worker.paused
        assert len(alerts) == 1 and "batch PAUSED" in alerts[0]
        assert sum(scraper.calls.values()) == 3, "pausing stops further scrapes"

        # While paused nothing is scraped until the probe is due.
        assert await worker.step() > 0
        assert sum(scraper.calls.values()) == 3

        # A probe that is still blocked burns no attempt and backs off further.
        worker.next_probe_at = 0
        before = worker.probe_delay
        await worker.step()
        probed = [i for i in items(job["job_id"]) if i["address_raw"] == "3 BLOCK ST"]
        attempts = get_connection().execute(
            "SELECT attempts FROM batch_items WHERE address_raw = '3 BLOCK ST'"
        ).fetchone()["attempts"]
        assert probed[0]["state"] == "pending" and attempts == 0
        assert worker.probe_delay == before * 2
        assert store.get_job(job["job_id"])["state"] == "paused"
        assert len(alerts) == 1, "still exactly one alert"

        # BC recovers: the next probe succeeds and the job resumes and finishes.
        scraper.default = None
        worker.next_probe_at = 0
        await worker.step()
        assert not worker.paused
        assert store.get_job(job["job_id"])["state"] == "running"
        assert len(alerts) == 2 and "resumed" in alerts[1]

        make_all_due()
        await run_until_idle(worker)
        final = store.get_job(job["job_id"])
        assert final["state"] == "completed" and final["paused_reason"] is None
        assert all(i["state"] == "done" for i in items(job["job_id"]))

    async def test_a_live_success_after_the_pause_resumes_the_job(
        self, scraper, worker, alerts, monkeypatch
    ):
        monkeypatch.setattr(settings, "batch_pause_after_blocked", 2)
        scraper.default = FailureType.BLOCKED
        job = await batch_service.submit(["1 X ST", "2 X ST", "3 X ST"])
        await worker.step()
        await worker.step()
        assert worker.paused

        await asyncio.sleep(0.05)  # so the success timestamp is strictly later
        browser_manager.record_success()  # e.g. a live /address lookup worked
        await worker.step()

        assert not worker.paused
        assert store.get_job(job["job_id"])["state"] == "running"

    async def test_a_timed_out_probe_does_not_resume(self, scraper, worker, alerts, monkeypatch):
        """Probe "until one succeeds": a timeout proves nothing about blocking."""
        monkeypatch.setattr(settings, "batch_pause_after_blocked", 2)
        scraper.default = FailureType.BLOCKED
        job = await batch_service.submit(["1 T ST", "2 T ST", "3 T ST"])
        await worker.step()
        await worker.step()
        assert worker.paused

        scraper.default = FailureType.TIMEOUT
        worker.next_probe_at = 0
        before = worker.probe_delay
        await worker.step()

        assert worker.paused
        assert worker.probe_delay == before * 2
        assert store.get_job(job["job_id"])["state"] == "paused"

    async def test_jobs_submitted_while_paused_start_paused(
        self, scraper, worker, alerts, monkeypatch
    ):
        monkeypatch.setattr(settings, "batch_pause_after_blocked", 1)
        scraper.default = FailureType.BLOCKED
        await batch_service.submit(["1 Y ST", "2 Y ST"])
        await worker.step()
        assert worker.paused

        later = await batch_service.submit(["9 LATER ST"])
        assert later["state"] == "paused"

    async def test_non_blocked_failures_reset_the_streak(
        self, scraper, worker, alerts, monkeypatch
    ):
        monkeypatch.setattr(settings, "batch_pause_after_blocked", 2)
        scraper.set("1 Z ST", FailureType.BLOCKED)
        scraper.set("2 Z ST", FailureType.NOT_FOUND)
        scraper.set("3 Z ST", FailureType.BLOCKED)
        await batch_service.submit(["1 Z ST", "2 Z ST", "3 Z ST"])
        for _ in range(3):
            await worker.step()
        assert not worker.paused


class TestRestart:
    async def test_restart_mid_job_finishes_every_item_without_rescraping(
        self, scraper, worker, alerts, monkeypatch
    ):
        addresses = [f"{n} RESTART ST" for n in range(5)]
        job = await batch_service.submit(addresses)

        await worker.step()
        await worker.step()
        # Simulate a crash with the third address claimed (in flight).
        store.claim_key(store.next_due_item(store.WORKABLE_JOB_STATES)["address_key"],
                        store.WORKABLE_JOB_STATES)
        assert store.job_progress(job["job_id"])["pending"] == 3

        restarted = batch_service.BatchWorker()
        monkeypatch.setattr(batch_service, "batch_worker", restarted)
        await restarted.start()
        try:
            deadline = time.monotonic() + 10
            while store.get_job(job["job_id"])["state"] != "completed":
                assert time.monotonic() < deadline, "job did not finish after restart"
                await asyncio.sleep(0.02)
        finally:
            await restarted.stop()

        assert store.job_progress(job["job_id"])["done"] == 5
        assert all(count == 1 for count in scraper.calls.values()), scraper.calls
        assert set(scraper.calls) == set(addresses)

    async def test_recovery_resumes_paused_jobs(self, scraper, worker):
        job = await batch_service.submit(["1 P ST"])
        store.set_job_states(store.WORKABLE_JOB_STATES, store.PAUSED, paused_reason="x")
        assert store.recover() == (0, 1)
        assert store.get_job(job["job_id"])["state"] == "running"


class TestSharingAndCancel:
    async def test_two_jobs_share_one_scrape_per_address(self, scraper, worker, alerts):
        a = await batch_service.submit(["1 SHARED ST", "1 ONLY A ST"])
        b = await batch_service.submit(["1 shared st"])
        await run_until_idle(worker)

        assert scraper.calls["1 SHARED ST"] == 1
        assert items(a["job_id"])[0]["result_json"] == items(b["job_id"])[0]["result_json"]
        assert store.get_job(b["job_id"])["state"] == "completed"

    async def test_worker_rechecks_the_cache_before_scraping(self, scraper, worker, alerts):
        job = await batch_service.submit(["1 FILLED ST"])
        cache_set("1 FILLED ST", AssessmentResult(total_value="$1"))
        await run_until_idle(worker)

        item = items(job["job_id"])[0]
        assert scraper.calls["1 FILLED ST"] == 0
        assert item["state"] == "done" and item["source"] == "cache"

    async def test_cancel_during_a_scrape_keeps_items_cancelled(
        self, scraper, worker, alerts, monkeypatch
    ):
        job = await batch_service.submit(["1 MID ST"])
        release = asyncio.Event()

        async def slow(address):
            await release.wait()
            return LookupOutcome.success(AssessmentResult(total_value="$5"))

        monkeypatch.setattr(lookup_mod, "_scrape_once", slow)
        step = asyncio.create_task(worker.step())
        await asyncio.sleep(0.05)
        await batch_service.cancel(job["job_id"])
        release.set()
        await step

        assert items(job["job_id"])[0]["state"] == "cancelled"
        assert lookup_mod.cache_get("1 MID ST") is not None, "the result still reaches the cache"


class TestRetentionAndLogs:
    async def test_old_finished_jobs_are_purged_active_ones_kept(self, scraper, worker):
        old_done = await batch_service.submit(["1 OLD ST"])
        await run_until_idle(worker)
        old_active = await batch_service.submit(["2 OLD ST"])
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE batch_jobs SET created_at = ?, completed_at = "
                "CASE WHEN completed_at IS NULL THEN NULL ELSE ? END",
                (ten_days_ago, ten_days_ago),
            )

        assert await batch_service.purge_expired() == 1
        assert store.get_job(old_done["job_id"]) is None
        assert items(old_done["job_id"]) == []
        assert store.get_job(old_active["job_id"]) is not None

    async def test_item_logs_carry_job_id_and_index(self, scraper, worker, caplog):
        caplog.set_level(logging.INFO, logger="app.services.batch")
        job = await batch_service.submit(["1 LOG ST", "2 LOG ST"])
        await run_until_idle(worker)

        lines = [r.getMessage() for r in caplog.records if "batch item" in r.getMessage()]
        assert any(f"job_id={job['job_id']} idx=1" in line for line in lines)

    async def test_stats_report_batch_counters(self, scraper, worker):
        from app.main import stats

        await batch_service.submit(["1 STAT ST"])
        await run_until_idle(worker)
        body = await stats()

        assert body["batch"]["jobs"] == {"completed": 1}
        assert body["batch"]["items"] == {"done": 1}
        assert "BC hourly (batch)" in body["rate_limit"]["tiers"]
        assert {"cache_calls", "bc_calls"} <= set(body)


async def test_500_cached_addresses_resolve_in_under_5_seconds(mock_provider, worker):
    addresses = [f"{n} BENCH ST VANCOUVER" for n in range(500)]
    for address in addresses:
        cache_set(lookup_mod.normalize_address(address), AssessmentResult(total_value="$1"))

    started = time.monotonic()
    job = await batch_service.submit(addresses)
    elapsed = time.monotonic() - started

    assert job["state"] == "completed" and job["cached"] == 500
    assert elapsed < 5, f"took {elapsed:.2f}s"
