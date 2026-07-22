"""SQLite-backed persistent cache for successful BC Assessment lookups.

Only valid results (those carrying a `total_value`) are ever written here;
failures live in `app.db.failures` so a bad scrape can never be served back as
if it were real data.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.db.connection import get_connection
from app.schemas import AssessmentResult

logger = logging.getLogger(__name__)


def _row_to_result(row: sqlite3.Row) -> AssessmentResult:
    return AssessmentResult(
        total_value=row["total_value"],
        year_built=row["year_built"],
        number_of_bed=row["number_of_bed"],
        number_of_baths=row["number_of_baths"],
        description=row["description"],
        land_size=row["land_size"],
        strata_area=row["strata_area"],
        building_storeys=row["building_storeys"],
    )


def _is_expired(created_at: str) -> bool:
    ttl = settings.scrape_cache_ttl_seconds
    if ttl <= 0:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() > ttl


def cache_get(address_key: str) -> Optional[AssessmentResult]:
    """Return cached assessment for a normalized address, or None on miss."""
    try:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT total_value, year_built, number_of_bed, number_of_baths,
                   description, land_size, strata_area, building_storeys,
                   created_at
            FROM assessment_cache
            WHERE address = ?
            """,
            (address_key,),
        ).fetchone()
    except sqlite3.Error:
        logger.exception("SQLite cache read failed for address=%r", address_key)
        return None

    if row is None:
        return None

    if _is_expired(row["created_at"]):
        logger.debug("SQLite cache expired for address=%r", address_key)
        cache_delete(address_key)
        return None

    return _row_to_result(row)


def cache_set(address_key: str, result: AssessmentResult) -> None:
    """Store a *valid* assessment result for a normalized address."""
    if not result.total_value:
        # Defensive: an entry without a total value is exactly the "invalid
        # result cached as success" failure mode this service used to have.
        logger.warning(
            "Refusing to cache result without total_value for address=%r", address_key
        )
        return

    created_at = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO assessment_cache (
                    address, total_value, year_built, number_of_bed,
                    number_of_baths, description, land_size, strata_area,
                    building_storeys, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    total_value = excluded.total_value,
                    year_built = excluded.year_built,
                    number_of_bed = excluded.number_of_bed,
                    number_of_baths = excluded.number_of_baths,
                    description = excluded.description,
                    land_size = excluded.land_size,
                    strata_area = excluded.strata_area,
                    building_storeys = excluded.building_storeys,
                    created_at = excluded.created_at
                """,
                (
                    address_key,
                    result.total_value,
                    result.year_built,
                    result.number_of_bed,
                    result.number_of_baths,
                    result.description,
                    result.land_size,
                    result.strata_area,
                    result.building_storeys,
                    created_at,
                ),
            )
    except sqlite3.Error:
        logger.exception("SQLite cache write failed for address=%r", address_key)


def cache_delete(address_key: str) -> None:
    """Remove a cache entry (e.g. after TTL expiry)."""
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "DELETE FROM assessment_cache WHERE address = ?", (address_key,)
            )
    except sqlite3.Error:
        logger.debug("Cache delete failed for address=%r", address_key, exc_info=True)


def cache_count() -> int:
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM assessment_cache").fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.Error:
        return 0
