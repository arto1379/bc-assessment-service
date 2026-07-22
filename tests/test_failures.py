"""Retry queue and negative-cache behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import failures
from app.db.connection import get_connection
from app.schemas import FailureType


def test_failure_is_recorded_with_debug_context():
    failures.record_failure(
        "7009 Tisdall St",
        "7009 TISDALL ST",
        FailureType.BLOCKED,
        reason="access denied",
        step="goto_home",
        url="https://www.bcassessment.ca/",
        snapshot_path="/tmp/snap.html",
    )

    items = failures.list_failures()
    assert len(items) == 1
    row = items[0]
    assert row["failure_type"] == "blocked"
    assert row["reason"] == "access denied"
    assert row["step"] == "goto_home"
    assert row["url"] == "https://www.bcassessment.ca/"
    assert row["html_snapshot_path"] == "/tmp/snap.html"
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None


def test_repeat_failures_increment_attempts_and_back_off():
    for _ in range(3):
        failures.record_failure("A", "A", FailureType.TIMEOUT, reason="slow")

    items = failures.list_failures()
    assert len(items) == 1, "the same address must not create duplicate rows"
    assert items[0]["attempts"] == 3


def test_success_resolves_the_entry():
    failures.record_failure("A", "A", FailureType.TIMEOUT, reason="slow")
    failures.mark_resolved("A")

    assert failures.list_failures() == []
    assert len(failures.list_failures(include_resolved=True)) == 1


def test_not_found_is_negatively_cached(monkeypatch):
    monkeypatch.setattr(settings, "not_found_cache_ttl_seconds", 3600)
    failures.record_failure("X", "X", FailureType.NOT_FOUND, reason="no suggestion")

    assert failures.get_negative_cache("X") == "no suggestion"


def test_scraper_faults_are_never_negatively_cached(monkeypatch):
    """Caching a block would turn a transient outage into a permanent wrong
    answer — precisely the failure mode this project is fixing."""
    monkeypatch.setattr(settings, "not_found_cache_ttl_seconds", 3600)

    for failure in (FailureType.BLOCKED, FailureType.TIMEOUT, FailureType.ERROR):
        failures.record_failure("Y", "Y", failure, reason="transient")
        assert failures.get_negative_cache("Y") is None


def test_negative_cache_expires(monkeypatch):
    monkeypatch.setattr(settings, "not_found_cache_ttl_seconds", 60)
    failures.record_failure("Z", "Z", FailureType.NOT_FOUND, reason="nope")

    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE failed_lookups SET last_failed_at = ? WHERE normalized_address = ?",
            (stale, "Z"),
        )

    assert failures.get_negative_cache("Z") is None


def test_negative_cache_disabled_when_ttl_zero(monkeypatch):
    monkeypatch.setattr(settings, "not_found_cache_ttl_seconds", 0)
    failures.record_failure("W", "W", FailureType.NOT_FOUND, reason="nope")

    assert failures.get_negative_cache("W") is None


def test_failure_counts_group_by_type():
    failures.record_failure("A", "A", FailureType.BLOCKED)
    failures.record_failure("B", "B", FailureType.BLOCKED)
    failures.record_failure("C", "C", FailureType.NOT_FOUND)

    assert failures.failure_counts() == {"blocked": 2, "not_found": 1}


def test_resolved_entries_drop_out_of_counts():
    failures.record_failure("A", "A", FailureType.BLOCKED)
    failures.mark_resolved("A")

    assert failures.failure_counts() == {}
