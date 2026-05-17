"""SQLite-backed persistent cache for BC Assessment scrape results."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.schemas import AssessmentResult

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _db_path() -> Path:
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent.parent / path
    return path


def _connect() -> sqlite3.Connection:
    db_path = _db_path()
    if not db_path.exists():
        raise FileNotFoundError(
            f"Cache database not found at {db_path}. "
            "Run: python scripts/setup_db.py"
        )
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
    return age_seconds > ttl


def cache_get(address_key: str) -> Optional[AssessmentResult]:
    """Return cached assessment for a normalized address, or None on miss."""
    try:
        with _lock:
            conn = _connect()
            try:
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
            finally:
                conn.close()
    except FileNotFoundError:
        logger.warning("SQLite cache unavailable; run scripts/setup_db.py")
        return None
    except sqlite3.Error:
        logger.exception("SQLite cache read failed for address=%r", address_key)
        return None

    if row is None:
        return None

    if _is_expired(row["created_at"]):
        logger.debug("SQLite cache expired for address=%r", address_key)
        cache_delete(address_key)
        return None

    logger.debug("SQLite cache hit for address=%r", address_key)
    return _row_to_result(row)


def cache_set(address_key: str, result: AssessmentResult) -> None:
    """Store assessment result for a normalized address."""
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        with _lock:
            conn = _connect()
            try:
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
                conn.commit()
            finally:
                conn.close()
    except FileNotFoundError:
        logger.warning("SQLite cache unavailable; run scripts/setup_db.py")
    except sqlite3.Error:
        logger.exception("SQLite cache write failed for address=%r", address_key)


def cache_delete(address_key: str) -> None:
    """Remove a cache entry (e.g. after TTL expiry)."""
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    "DELETE FROM assessment_cache WHERE address = ?",
                    (address_key,),
                )
                conn.commit()
            finally:
                conn.close()
    except (FileNotFoundError, sqlite3.Error):
        pass
