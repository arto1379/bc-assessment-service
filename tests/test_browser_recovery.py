"""Browser recycling policy and alert de-duplication.

The recycling policy is the fix for the "works for two hours then degrades"
behaviour, so `_needs_restart` is exercised directly with a stub browser rather
than launching real Chromium.
"""

from __future__ import annotations

from app.config import settings
from app.schemas import FailureType, LookupOutcome
from app.services.browser import BrowserManager
from app.services.stats import notify_failure, notify_recovered


class FakeBrowser:
    def __init__(self, connected: bool = True):
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


def make_manager(connected: bool = True) -> BrowserManager:
    manager = BrowserManager()
    manager._browser = FakeBrowser(connected)
    return manager


class TestRecyclingPolicy:
    def test_healthy_browser_is_not_recycled(self):
        manager = make_manager()
        needs, _ = manager._needs_restart()
        assert needs is False

    def test_missing_browser_needs_launch(self):
        manager = BrowserManager()
        needs, reason = manager._needs_restart()
        assert needs is True and reason == "no browser"

    def test_disconnected_browser_is_recycled(self):
        manager = make_manager(connected=False)
        needs, reason = manager._needs_restart()
        assert needs is True and "disconnected" in reason

    def test_browser_is_recycled_after_the_lookup_budget(self, monkeypatch):
        monkeypatch.setattr(settings, "browser_max_lookups", 5)
        manager = make_manager()
        for _ in range(5):
            manager.record_success()

        needs, reason = manager._needs_restart()
        assert needs is True and "5 lookups" in reason

    def test_zero_budget_disables_lookup_based_recycling(self, monkeypatch):
        monkeypatch.setattr(settings, "browser_max_lookups", 0)
        manager = make_manager()
        for _ in range(1000):
            manager.record_success()

        assert manager._needs_restart()[0] is False

    def test_repeated_failures_trigger_a_restart(self, monkeypatch):
        monkeypatch.setattr(settings, "browser_restart_after_failures", 3)
        manager = make_manager()

        for _ in range(2):
            manager.record_failure(FailureType.TIMEOUT)
        assert manager._needs_restart()[0] is False

        manager.record_failure(FailureType.TIMEOUT)
        assert manager._needs_restart()[0] is True

    def test_success_clears_the_failure_streak(self, monkeypatch):
        monkeypatch.setattr(settings, "browser_restart_after_failures", 3)
        manager = make_manager()

        manager.record_failure(FailureType.TIMEOUT)
        manager.record_failure(FailureType.TIMEOUT)
        manager.record_success()

        assert manager.health.consecutive_failures == 0
        assert manager._needs_restart()[0] is False


class TestHealthState:
    def test_becomes_unhealthy_after_the_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "unhealthy_after_failures", 3)
        manager = make_manager()

        assert manager.is_healthy is True
        for _ in range(3):
            manager.record_failure(FailureType.BLOCKED)
        assert manager.is_healthy is False

    def test_health_snapshot_records_the_last_failure_type(self):
        manager = make_manager()
        manager.record_failure(FailureType.BLOCKED)

        snapshot = manager.health.as_dict()
        assert snapshot["last_failure_type"] == "blocked"
        assert snapshot["consecutive_failures"] == 1
        assert snapshot["last_failure_at"] is not None


class TestAlertDeduplication:
    async def test_repeat_alerts_of_one_type_are_suppressed(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.services.stats.send_telegram_message_background", sent.append
        )
        monkeypatch.setattr(settings, "alert_min_interval_seconds", 300)

        outcome = LookupOutcome.failed(FailureType.BLOCKED, "denied", url="u", step="s")
        for _ in range(5):
            await notify_failure("7009 TISDALL ST", outcome)

        assert len(sent) == 1, "a block storm must not send one alert per request"

    async def test_alert_carries_every_field_the_prd_requires(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.services.stats.send_telegram_message_background", sent.append
        )

        outcome = LookupOutcome.failed(
            FailureType.BLOCKED,
            "access denied",
            url="https://www.bcassessment.ca/",
            step="goto_home",
            restarted=True,
            snapshot_path="/tmp/s.html",
        )
        await notify_failure("7009 TISDALL ST", outcome)

        message = sent[0]
        assert "7009 TISDALL ST" in message      # address
        assert "blocked" in message               # failure type
        assert "bcassessment.ca" in message       # url
        assert "access denied" in message         # reason
        assert "Timestamp:" in message            # timestamp
        assert "Browser restarted: yes" in message
        assert "Suggested action:" in message

    async def test_distinct_failure_types_alert_separately(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.services.stats.send_telegram_message_background", sent.append
        )

        await notify_failure("A", LookupOutcome.failed(FailureType.BLOCKED, "x"))
        await notify_failure("A", LookupOutcome.failed(FailureType.TIMEOUT, "y"))

        assert len(sent) == 2

    async def test_not_found_does_not_raise_a_scraper_alert(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.services.stats.send_telegram_message_background", sent.append
        )

        await notify_failure("A", LookupOutcome.failed(FailureType.NOT_FOUND, "nope"))

        assert sent == []

    async def test_recovery_notice_is_sent_once(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(
            "app.services.stats.send_telegram_message_background", sent.append
        )

        await notify_failure("A", LookupOutcome.failed(FailureType.BLOCKED, "x"))
        await notify_recovered("A")
        await notify_recovered("A")

        assert sum("recovered" in m for m in sent) == 1
