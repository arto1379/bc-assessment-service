"""SQLite cache behaviour, including the rule that invalid results never land."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.cache import cache_count, cache_delete, cache_get, cache_set
from app.db.connection import get_connection
from app.schemas import AssessmentResult

VALID = AssessmentResult(total_value="$817,000", year_built="1989", number_of_bed="2")


def test_miss_returns_none():
    assert cache_get("7009 TISDALL ST VANCOUVER") is None


def test_roundtrip():
    cache_set("7009 TISDALL ST VANCOUVER", VALID)
    got = cache_get("7009 TISDALL ST VANCOUVER")

    assert got is not None
    assert got.total_value == "$817,000"
    assert got.year_built == "1989"


def test_upsert_does_not_duplicate():
    cache_set("A ST", VALID)
    cache_set("A ST", AssessmentResult(total_value="$900,000"))

    assert cache_count() == 1
    assert cache_get("A ST").total_value == "$900,000"


def test_result_without_total_value_is_never_cached():
    """The old bug: an empty scrape stored as if it were a real result."""
    cache_set("EMPTY ST", AssessmentResult(total_value=None, year_built="1989"))

    assert cache_get("EMPTY ST") is None
    assert cache_count() == 0


def test_delete():
    cache_set("B ST", VALID)
    cache_delete("B ST")
    assert cache_get("B ST") is None


def test_entry_expires_once_past_ttl(monkeypatch):
    monkeypatch.setattr(settings, "scrape_cache_ttl_seconds", 60)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO assessment_cache (address, total_value, created_at) "
            "VALUES (?, ?, ?)",
            ("OLD ST", "$1", stale),
        )

    assert cache_get("OLD ST") is None


def test_entry_within_ttl_survives(monkeypatch):
    monkeypatch.setattr(settings, "scrape_cache_ttl_seconds", 3600)
    cache_set("FRESH ST", VALID)
    assert cache_get("FRESH ST") is not None


def test_ttl_zero_means_never_expire(monkeypatch):
    monkeypatch.setattr(settings, "scrape_cache_ttl_seconds", 0)
    ancient = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()

    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO assessment_cache (address, total_value, created_at) "
            "VALUES (?, ?, ?)",
            ("ANCIENT ST", "$1", ancient),
        )

    assert cache_get("ANCIENT ST") is not None
