"""
Database access layer for ipo_database.db.

Two jobs:
1. Look up a company by name (exact match first, then fuzzy) -- same
   normalize_name() logic as predict_by_name.py / merge_all.py, so lookups
   stay consistent with the rest of the project.
2. Upsert a full IPORecord -- used both by the original backfill process and
   by the live-fetch path, so a freshly-fetched company lands in the DB with
   the exact same shape as every existing row.
"""

import re
import sqlite3
import difflib
from pathlib import Path
from typing import Optional

from .schemas import IPORecord, IPO_COLUMNS

DB_PATH = Path(__file__).resolve().parent.parent / "ipo_database.db"

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
    conn = sqlite3.connect(DB_PATH)
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
