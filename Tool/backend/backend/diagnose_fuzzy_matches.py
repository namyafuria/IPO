"""
diagnose_fuzzy_matches.py

The earlier clear_stale_listing_dates.py assumed a row literally named
e.g. "Behari Lal Engineering" existed in ipo_master_records and was stale.
It doesn't -- all 5 names came back [NOT FOUND], even with a loose LIKE
match. But live_fetch.py's log line still says "already listed on
2024-10-15" for that exact input name.

That log line prints the *input* company_name, not the DB's actual stored
name -- and db.find_company() falls through to a FUZZY match (difflib,
cutoff=0.6) when there's no exact match. This project has a documented
history of that cutoff producing false positives (e.g. "Suich Industries"
vs "Sigachi Industries" from an earlier session). This script reproduces
db.py's exact matching logic (normalize_name + difflib) so we can see
which real DB row each of these 5 names is actually resolving to.

Usage:
    python diagnose_fuzzy_matches.py

Run from the same folder as clear_stale_listing_dates.py (backend root).
Read-only -- does not change anything in the DB.
"""

import difflib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402

NAMES_TO_CHECK = [
    "Behari Lal Engineering",
    "Lalithaa Jewellery Mart",
    "Horizon Industrial Parks",
    "Gaja Alternative Asset Management",
    "Skyways Air Services",
]

# Identical to db.py's normalize_name() / SUFFIXES -- must stay in sync,
# since the whole point is reproducing find_company()'s exact behavior.
SUFFIXES = {
    "limited", "ltd.", "ltd", "private", "pvt.", "pvt",
    "incorporated", "inc.", "inc", "corp.", "corp", "company", "co.",
}


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"\(.*?\)", "", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = [t for t in n.split() if t not in SUFFIXES]
    return " ".join(tokens).strip()


def main():
    db_path = Path(config.DB_PATH)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent / db_path

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT company_name, listing_date, open_date, close_date, allotment_date "
        "FROM ipo_master_records"
    )
    rows = cur.fetchall()
    conn.close()

    names = [r["company_name"] for r in rows]
    norm_names = [normalize_name(n) for n in names]

    for query in NAMES_TO_CHECK:
        target = normalize_name(query)
        print(f"\n=== {query!r} (normalized: {target!r}) ===")

        # Exact match check (same as find_company's first pass)
        exact_idx = next((i for i, n in enumerate(norm_names) if n == target), None)
        if exact_idx is not None:
            row = rows[exact_idx]
            print(f"  EXACT MATCH: {row['company_name']!r} -- listing_date={row['listing_date']!r}")
            continue

        # Fuzzy match check (same cutoff as find_company: 0.6)
        close = difflib.get_close_matches(target, norm_names, n=3, cutoff=0.6)
        if not close:
            print("  No exact match and NO fuzzy match >= 0.6 either -- "
                  "find_company() would return None for this name. The "
                  "'already listed' log line must be coming from a "
                  "different cause -- worth re-checking against the live "
                  "Render logs/DB directly.")
            continue

        for match in close:
            idx = norm_names.index(match)
            row = rows[idx]
            score = difflib.SequenceMatcher(None, target, match).ratio()
            print(f"  FUZZY MATCH (score={score:.2f}): {row['company_name']!r} "
                  f"-- listing_date={row['listing_date']!r}, open_date={row['open_date']!r}, "
                  f"close_date={row['close_date']!r}, allotment_date={row['allotment_date']!r}")


if __name__ == "__main__":
    main()
