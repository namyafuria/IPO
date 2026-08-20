"""Batch-corrects ipo_master_records.listing_date using the NSE SME
(Emerge) source dates from date_mismatch_near_misses.csv.

Root cause of the mismatch batch (2026-08-20 diagnosis, see chat/memory):
225/228 DB listing_date values land exactly on the 15th of their month
(2025-12-15, 2025-10-15, ...) while the real NSE listing dates are
scattered (30-Dec-25, 01-Oct-25, ...) -- a placeholder-date bug somewhere
upstream of this table, not a matcher bug. 2 more had db_listing_date=None
entirely. 1 outlier (Radiowalla Network, db=2024-07-08 vs real 2024-04-05)
manually confirmed correct via web search (Chittorgarh/Goodreturns/NSE
listing pages all agree on 2024-04-05) -- also included, same fix.

All 228 rows here were NAME-MATCHED already by nse_bse_symbol_match.py's
NSE_SME branch (exact/prefix name match, symbol confirmed) -- this script
does NOT redo that matching, it trusts the near-miss CSV's company_name
column (which is the DB's own value) and only rewrites listing_date.

Default: dry run, prints planned updates, writes nothing.
--commit: actually writes, after taking a DB backup copy first.
"""
import argparse
import csv
import re
import shutil
import sqlite3
import sys
from datetime import datetime


def parse_nse_date(s: str) -> str:
    """'30-Dec-25' -> '2025-12-30' (ISO), matching the DB's own listing_date
    format seen throughout (e.g. '2025-12-15')."""
    return datetime.strptime(s.strip(), "%d-%b-%y").date().isoformat()


def load_corrections(csv_path: str) -> list[dict]:
    out = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            detail = r["detail"]
            m = re.search(r"db=(None|'[\d-]+')\s+vs\s+nse_sme='([\d\w-]+)'", detail)
            if not m:
                print(f"SKIP (unparseable detail): {r['company_name']!r}: {detail!r}", file=sys.stderr)
                continue
            db_val, nse_val = m.group(1), m.group(2)
            try:
                new_date = parse_nse_date(nse_val)
            except ValueError:
                print(f"SKIP (bad NSE date {nse_val!r}): {r['company_name']!r}", file=sys.stderr)
                continue
            out.append({
                "company_name": r["company_name"],
                "old_listing_date": None if db_val == "None" else db_val.strip("'"),
                "new_listing_date": new_date,
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="path to date_mismatch_near_misses.csv")
    ap.add_argument("--db-path", required=True, help="path to ipo_database.db")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args()

    corrections = load_corrections(args.csv)
    print(f"Loaded {len(corrections)} corrections from {args.csv}")

    conn = sqlite3.connect(args.db_path)
    cur = conn.cursor()

    no_db_match = []
    multi_db_match = []
    would_update = []

    for c in corrections:
        cur.execute(
            "SELECT rowid, listing_date FROM ipo_master_records WHERE company_name = ?",
            (c["company_name"],),
        )
        rows = cur.fetchall()
        if len(rows) == 0:
            no_db_match.append(c["company_name"])
            continue
        if len(rows) > 1:
            multi_db_match.append(c["company_name"])
            continue
        rowid, current_listing_date = rows[0]
        would_update.append({
            **c,
            "rowid": rowid,
            "current_in_db": current_listing_date,
        })

    print(f"\nWould update: {len(would_update)}")
    print(f"No DB match (company_name not found as-is): {len(no_db_match)}")
    print(f"Multiple DB match (ambiguous, skipped): {len(multi_db_match)}")

    if no_db_match:
        print("\n  no_db_match sample:", no_db_match[:5])
    if multi_db_match:
        print("  multi_db_match sample:", multi_db_match[:5])

    print("\nSample of planned updates (first 10):")
    for c in would_update[:10]:
        print(f"  {c['company_name']!r}: {c['current_in_db']!r} -> {c['new_listing_date']!r}")

    if not args.commit:
        print("\nDRY RUN — no writes made. Re-run with --commit to apply.")
        conn.close()
        return

    backup_path = args.db_path + ".backup_pre_listing_date_fix"
    shutil.copy2(args.db_path, backup_path)
    print(f"\nDB backed up to {backup_path}")

    written = 0
    for c in would_update:
        cur.execute(
            "UPDATE ipo_master_records SET listing_date = ? WHERE rowid = ?",
            (c["new_listing_date"], c["rowid"]),
        )
        written += 1
    conn.commit()
    conn.close()
    print(f"\nCOMMIT done: written={written}")


if __name__ == "__main__":
    sys.exit(main())
