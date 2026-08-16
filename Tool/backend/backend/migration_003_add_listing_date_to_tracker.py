"""
migration_003_add_listing_date_to_tracker.py

Adds two nullable columns to ipo_live_tracker: listing_date, allotment_date.

WHY: ipoji.py's parse_details_page() has always parsed both off ipoji.com's
per-company page, but upsert_live_tracker() silently dropped them (flagged
as "assumption #1" at the top of ipoji.py since Step 2). Without
listing_date stored here, /ipos/open and /ipos/awaiting-allotment
(routers_live.py) had no reliable way to exclude a company that had
already listed -- they could only filter on close_date, which is either
unparsed for some pages or simply doesn't update the instant a company
lists. Root cause of already-listed companies still showing up in the
Open tab (confirmed 2026-08-16).

Run this ONCE against the real DB, before deploying the updated ipoji.py
/ routers_live.py. Idempotent -- safe to run again (checks column
existence first via PRAGMA table_info, like the project's other
migrations).

Usage:
    python migration_003_add_listing_date_to_tracker.py [path/to/ipo_database.db]

Defaults to app.config.DB_PATH if no path is given and the app package is
importable from cwd; otherwise pass the path explicitly.
"""

import sqlite3
import sys


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def run(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cols = _existing_columns(conn, "ipo_live_tracker")
        added = []
        if "listing_date" not in cols:
            conn.execute("ALTER TABLE ipo_live_tracker ADD COLUMN listing_date TEXT")
            added.append("listing_date")
        if "allotment_date" not in cols:
            conn.execute("ALTER TABLE ipo_live_tracker ADD COLUMN allotment_date TEXT")
            added.append("allotment_date")
        conn.commit()
        if added:
            print(f"Added columns to ipo_live_tracker: {', '.join(added)}")
        else:
            print("ipo_live_tracker already has listing_date and allotment_date -- nothing to do.")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        try:
            from app import config  # type: ignore
            path = config.DB_PATH
        except Exception:
            print("Usage: python migration_003_add_listing_date_to_tracker.py <path/to/ipo_database.db>")
            sys.exit(1)
    run(path)
    print("Existing rows have listing_date/allotment_date = NULL until the next poll refreshes them "
          "-- /ipos/open falls back to close_date-only filtering for those until then.")
