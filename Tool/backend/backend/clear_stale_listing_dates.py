"""
clear_stale_listing_dates.py

One-off cleanup: clears listing_date/open_date/close_date/allotment_date
for a specific list of companies whose ipo_master_records row is stale
data from an earlier backfill/fuzzy-match pass -- NOT the same IPO as the
one currently live on ipoji/ipogyani under that name.

Confirmed case: "Behari Lal Engineering" -- the DB has listing_date=
2024-10-15, but the company's August 2026 IPO is its actual maiden
(first-ever) public listing (confirmed via web search against Chittorgarh/
Axis Direct/Paytm Money sources). The 2024 date is simply wrong data, not
a different real company.

Why this matters: live_fetch.py's _already_listed() checks listing_date
and, if it's set and in the past, skips fetching fresh ipogyani data for
that company entirely (treats it as "frozen, already listed"). With a
bogus listing_date sitting in the DB, the real, current IPO for that
company can never get its live data fetched -- every sync cycle just
logs "Skipping ipogyani fetch... pre-listing data is frozen" forever.

This script does NOT delete the rows (that would also lose sector/
price-band/etc. fields that may still be valid) -- it only clears the
four date fields that _already_listed() and is_still_trackable() gate on,
so the next sync treats these companies as not-yet-listed and pulls fresh
real data, overwriting whatever's stale.

Usage:
    python clear_stale_listing_dates.py            # dry run, prints what would change
    python clear_stale_listing_dates.py --apply     # actually clears the fields

Run this from the backend root (same folder as config.py / db.py).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402  -- adjust import if your package layout differs

# Companies confirmed (or strongly suspected, per the same symptom pattern)
# to have a stale/incorrect listing_date blocking their real, current IPO
# from ever being fetched. Add/remove names here as you confirm each one.
STALE_COMPANIES = [
    "Behari Lal Engineering",
    "Lalithaa Jewellery Mart",
    "Horizon Industrial Parks",
    "Gaja Alternative Asset Management",
    "Skyways Air Services",
]

FIELDS_TO_CLEAR = ["listing_date", "open_date", "close_date", "allotment_date"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually clear the fields (default is dry-run)")
    args = parser.parse_args()

    db_path = Path(config.DB_PATH)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent / db_path

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        found_any = False
        for name in STALE_COMPANIES:
            row = cur.execute(
                "SELECT company_name, listing_date, open_date, close_date, allotment_date "
                "FROM ipo_master_records WHERE company_name = ?",
                (name,),
            ).fetchone()
            if row is None:
                # Try a loose match in case the stored name has a suffix
                # (e.g. "Behari Lal Engineering Ltd.") -- report it rather
                # than silently skip, so you can add the exact name above.
                like_rows = cur.execute(
                    "SELECT company_name FROM ipo_master_records WHERE company_name LIKE ?",
                    (f"%{name}%",),
                ).fetchall()
                if like_rows:
                    print(f"[NOT AN EXACT MATCH] '{name}' not found verbatim, but similar row(s) exist: "
                          f"{[r['company_name'] for r in like_rows]} -- update STALE_COMPANIES with the exact name.")
                else:
                    print(f"[NOT FOUND] '{name}' -- no row in ipo_master_records at all, nothing to clear.")
                continue

            found_any = True
            print(f"[FOUND] {row['company_name']!r}: "
                  f"listing_date={row['listing_date']!r}, open_date={row['open_date']!r}, "
                  f"close_date={row['close_date']!r}, allotment_date={row['allotment_date']!r}")

            if args.apply:
                set_clause = ", ".join(f"{f} = NULL" for f in FIELDS_TO_CLEAR)
                cur.execute(
                    f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = ?",
                    (row["company_name"],),
                )
                print(f"    -> cleared {FIELDS_TO_CLEAR}")

        if args.apply:
            conn.commit()
            print("\nDone. Changes committed. Next sync cycle should pull fresh live data for these companies.")
        else:
            if found_any:
                print("\nDry run only -- nothing was changed. Re-run with --apply to actually clear these fields.")
            else:
                print("\nDry run only -- no matching rows found for any listed company name.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
