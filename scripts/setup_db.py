#!/usr/bin/env python3
"""One-time setup: create the SQLite cache database and tables.

Run once before starting the API:

    python scripts/setup_db.py

Optional custom path:

    python scripts/setup_db.py /path/to/bc_assessment.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "bc_assessment.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assessment_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL UNIQUE COLLATE NOCASE,
    total_value TEXT,
    year_built TEXT,
    number_of_bed TEXT,
    number_of_baths TEXT,
    description TEXT,
    land_size TEXT,
    strata_area TEXT,
    building_storeys TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_cache_address ON assessment_cache(address);
"""


def setup_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    db_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DB
    setup_database(db_path)
    print(f"Cache database ready: {db_path}")


if __name__ == "__main__":
    main()
