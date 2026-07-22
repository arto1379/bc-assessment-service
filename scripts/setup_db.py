#!/usr/bin/env python3
"""One-time setup: create the SQLite cache database and tables.

Run once before starting the API:

    python scripts/setup_db.py

Optional custom path:

    python scripts/setup_db.py /path/to/bc_assessment.db

Safe to re-run: the schema is CREATE TABLE IF NOT EXISTS throughout, so this
also acts as the migration that adds `failed_lookups` to an existing database
without touching cached results.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db.connection import db_path, init_db  # noqa: E402


def main() -> None:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else db_path()
    existed = target.exists()
    init_db(target)

    conn = sqlite3.connect(target)
    try:
        tables = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
        cached = conn.execute("SELECT COUNT(*) FROM assessment_cache").fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM failed_lookups WHERE resolved_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    action = "migrated" if existed else "created"
    print(f"Cache database {action}: {target}")
    print(f"  tables: {', '.join(tables)}")
    print(f"  cached addresses: {cached}")
    print(f"  unresolved failures: {queued}")


if __name__ == "__main__":
    main()
