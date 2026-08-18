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


# --- strict fuzzy matcher (identical logic to gmp_sync.py's strict_match,
# duplicated here rather than cross-imported -- same "kept in sync on
# purpose" convention as normalize_name() above) ---
#
# FIX (this session): find_company()'s fallback used to be
# difflib.get_close_matches() at a raw character-similarity cutoff of 0.6.
# Confirmed via diagnose_fuzzy_matches.py that this matched almost entirely
# on generic shared suffix words rather than the distinctive part of a
# company's name -- e.g. "Behari Lal Engineering" (a real, unrelated,
# brand-new IPO) fuzzy-matched to "TechEra Engineering Limited";
# "Gaja Alternative Asset Management" matched to "UTI Asset Management
# Company Ltd". This silently merged live-fetch data for one company into
# a completely different company's DB row -- e.g. writing/reading a
# 2024-10-15 listing_date that actually belonged to an unrelated company,
# which then made live_fetch.py's _already_listed() check wrongly treat
# a brand-new IPO as already listed and permanently skip fetching its
# real data.
#
# Replaced with strict_match: requires 2+ significant words (or one word
# >= 6 chars) and a real word-boundary substring match, not a raw
# similarity score. This is intentionally less typo-tolerant than the old
# difflib fallback -- a genuinely misspelled search query may now return
# "not found" instead of a guess -- traded off deliberately in favor of
# never silently attaching one company's data to a different company's row.
_STRICT_SUFFIXES_RE = re.compile(r"\b(limited|ltd|private|pvt|company|co|the|formerly|india)\b", re.IGNORECASE)


def _strict_normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = _STRICT_SUFFIXES_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strict_match(name: str, db_names: list[str]) -> Optional[str]:
    n = _strict_normalize(name)
    toks = n.split()
    if not toks:
        return None
    significant = len(toks) >= 2 or (len(toks) == 1 and len(toks[0]) >= 6)
    if not significant:
        return None
    pattern = r"\b" + re.escape(n) + r"\b"
    candidates = set()
    for db_name in db_names:
        dbn = _strict_normalize(db_name)
        if dbn and (re.search(pattern, dbn) or re.search(r"\b" + re.escape(dbn) + r"\b", n)):
            candidates.add(db_name)
    return next(iter(candidates)) if len(candidates) == 1 else None


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


def company_exists_exact(company_name: str) -> bool:
    """Exact-string existence check against ipo_master_records.company_name
    -- deliberately NOT normalize_name()/strict_match()-based like
    find_company(). For callers that already hold a canonical company_name
    pulled straight from a prior query against this same table (e.g.
    bhavcopy_sync.get_trackable_companies() rows, in
    scheduler.save_trajectory_predictions_for()) -- re-running that value
    through find_company()'s fuzzy normalize/substring matching would add
    real risk for zero benefit: if two rows ever normalize to the same
    string (a known unresolved case in this project -- see the Accent
    Microcell/AMIC Forging SC_CODE conflict), find_company() can return a
    DIFFERENT row than the one the caller actually meant, silently
    attaching a fresh save to the wrong company. An exact string match has
    no such ambiguity -- it either is that literal row or it isn't."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT 1 FROM ipo_master_records WHERE company_name = ? LIMIT 1",
            (company_name,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def find_company(query: str) -> tuple[Optional[IPORecord], bool]:
    """Returns (record, exact_match). record is None if nothing matched --
    see strict_match()'s docstring above for why the old difflib-based
    fuzzy fallback was replaced."""
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
        matched_name = strict_match(query, names)
        if matched_name is not None:
            idx = names.index(matched_name)
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
