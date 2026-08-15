"""
Database access layer for ipo_database.db.

Two jobs:
1. Look up a company by name (exact match first, then fuzzy) -- same
   normalize_name() logic as predict_by_name.py / merge_all.py, so lookups
   stay consistent with the rest of the project.
2. Upsert a full IPORecord -- used both by the original backfill process and
   by the live-fetch path, so a freshly-fetched company lands in the DB with
   the exact same shape as every existing row.

--- FIX LOG (2026-08-12) ---
DB_PATH used to be hardcoded here, independently of config.py:
    DB_PATH = Path(__file__).resolve().parent.parent / "ipo_database.db"
That meant this module and gmp_sync.py / the /api/sync_and_predict route
could resolve to two different files on disk depending on how config.DB_PATH
was set (e.g. via the DB_PATH env var) vs wherever this hardcoded path
landed. Nothing would crash -- sync would write to one file, predictions
would read from another, and results would just silently never reflect a
sync. Fixed by importing config.DB_PATH as the single source of truth, same
as every other module in this project already does.
"""

import re
import sqlite3
import difflib
from pathlib import Path
from typing import Optional

from . import config
from .schemas import IPORecord, IPO_COLUMNS

# config.DB_PATH may be a relative filename (e.g. "ipo_database.db") or an
# absolute path (e.g. via the DB_PATH env var). Resolve relative paths
# against the backend root (one level up from this file), matching where
# the old hardcoded default pointed -- so nothing moves for anyone who
# hasn't set DB_PATH explicitly.
_configured = Path(config.DB_PATH)
DB_PATH = _configured if _configured.is_absolute() else (
    Path(__file__).resolve().parent.parent / _configured
)

SUFFIXES = {
    "limited", "ltd.", "ltd", "private", "pvt.", "pvt",
    "incorporated", "inc.", "inc", "corp.", "corp", "company", "co.",
}


def normalize_name(name: str) -> str:
    """Identical logic to predict_by_name.py's normalize_name(), kept in sync
    on purpose -- lookups here and predictions there must agree on identity."""
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"\(.*?\)", "", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = [t for t in n.split() if t not in SUFFIXES]
    return " ".join(tokens).strip()


def get_connection() -> sqlite3.Connection:
    # timeout=30 + WAL: this is the busiest connection path in the project
    # (every /api/company, /api/predict*, and upsert_record call goes
    # through here) and it runs concurrently with the scheduler's own
    # writes -- without this, any request landing mid-sync gets
    # "database is locked" instead of waiting the brief contention out.
    # See routers_live.py's _get_conn() and gmp_sync.py's run_gmp_sync()
    # for the same fix applied to the other two places that open
    # connections against this file.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def find_company(query: str) -> tuple[Optional[IPORecord], bool]:
    """Returns (record, exact_match). record is None if nothing close enough
    was found (fuzzy cutoff 0.6, same threshold predict_by_name.py uses)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(IPO_COLUMNS)} FROM ipo_master_records")
        rows = cur.fetchall()

        target = normalize_name(query)
        for row in rows:
            if normalize_name(row["company_name"]) == target:
                return IPORecord(**dict(row)), True

        names = [row["company_name"] for row in rows]
        norm_names = [normalize_name(n) for n in names]
        close = difflib.get_close_matches(target, norm_names, n=1, cutoff=0.6)
        if close:
            idx = norm_names.index(close[0])
            return IPORecord(**dict(rows[idx])), False

        return None, False
    finally:
        conn.close()


def find_live_and_recent_companies(track_days: int) -> list[str]:
    """Company names currently open for bidding, or listed within the last
    `track_days` days -- the 'still relevant right now' set used by
    /api/sync_and_predict in main.py. Lives here (not as raw SQL in main.py)
    so it goes through the same DB_PATH/connection as everything else in
    this module rather than risking a second, possibly-divergent path."""
    conn = get_connection()
    try:
        from datetime import date, timedelta
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=track_days)).isoformat()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT company_name
            FROM ipo_master_records
            WHERE (open_date <= ? AND close_date >= ?)
               OR (listing_date IS NOT NULL AND listing_date >= ? AND listing_date <= ?)
            ORDER BY COALESCE(listing_date, close_date) DESC
            """,
            (today, today, cutoff, today),
        )
        return [row["company_name"] for row in cur.fetchall()]
    finally:
        conn.close()


def upsert_record(record: IPORecord) -> None:
    """Insert a new row, or update it in place if a row with the same
    (normalized) company_name already exists. Full-row replace on update --
    a live refresh is expected to supply the complete picture, not a patch."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT company_name FROM ipo_master_records")
        existing_names = [r["company_name"] for r in cur.fetchall()]
        target = normalize_name(record.company_name)
        match = next((n for n in existing_names if normalize_name(n) == target), None)

        data = record.model_dump()
        if match:
            set_clause = ", ".join(f"{col} = :{col}" for col in IPO_COLUMNS if col != "company_name")
            data["match_name"] = match
            cur.execute(
                f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = :match_name",
                data,
            )
        else:
            placeholders = ", ".join(f":{col}" for col in IPO_COLUMNS)
            cur.execute(
                f"INSERT INTO ipo_master_records ({', '.join(IPO_COLUMNS)}) VALUES ({placeholders})",
                data,
            )
        conn.commit()
    finally:
        conn.close()
