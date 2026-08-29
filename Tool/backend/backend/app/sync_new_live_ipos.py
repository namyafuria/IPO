"""
Diffs ipo_database.db's ipo_live_tracker (populated by your IPO Ji live
fetcher) against ipoguru_kpis.db, and fetches KPI data for any company
that's live/upcoming but not yet KPI-scraped.

Covers BOTH asks:
  - run this right after your IPO Ji live-update step -> any brand-new
    IPO it just added gets KPI-fetched same run.
  - covers backlog too: currently-open IPOs that predate this script
    (ipoguru_kpis.db was built from the *performance* pages, which only
    list already-listed IPOs - an IPO still open right now was never on
    those pages, so it's missing until this script catches it).

Usage:
    python3 sync_new_live_ipos.py ipo_database.db ipoguru_kpis.db
    python3 sync_new_live_ipos.py ipo_database.db ipoguru_kpis.db --dry-run

Matching: same normalize() as match_and_update_kpis.py (strip Ltd/Pvt/
IPO/noise words, lowercase) - a live_tracker company is considered
"already covered" if its normalized name exactly equals a normalized
name already in ipoguru_kpis.db. (Exact-normalized only here, not the
fuzzy substring match - this is a "do we already have it at all" check,
not a cross-database join, so exact is the safer default; a near-miss
just means one extra harmless re-fetch.)
"""

import argparse
import sqlite3
import time

from match_and_update_kpis import normalize
from fetch_new_ipo_kpi import fetch_one
from ipoguru_scraper import init_db, upsert


def already_covered(company_name: str, covered_norms: set[str]) -> bool:
    return normalize(company_name) in covered_norms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("master_db", help="ipo_database.db (has ipo_live_tracker)")
    parser.add_argument("kpi_db", help="ipoguru_kpis.db")
    parser.add_argument("--dry-run", action="store_true",
                         help="only print what would be fetched")
    args = parser.parse_args()

    master_conn = sqlite3.connect(args.master_db)
    kpi_conn = sqlite3.connect(args.kpi_db)

    live_rows = master_conn.execute(
        "SELECT DISTINCT company_name, issue_category FROM ipo_live_tracker"
    ).fetchall()

    existing_names = [r[0] for r in kpi_conn.execute(
        "SELECT name FROM ipo_guru_kpis WHERE name IS NOT NULL").fetchall()]
    covered_norms = {normalize(n) for n in existing_names}

    to_fetch = []
    for company_name, issue_category in live_rows:
        if already_covered(company_name, covered_norms):
            continue
        category = "sme" if (issue_category or "").lower() == "sme" else "mainboard"
        to_fetch.append((company_name, category))

    print(f"Live-tracker companies: {len(live_rows)}")
    print(f"Already have KPI data: {len(live_rows) - len(to_fetch)}")
    print(f"Missing, need fetch: {len(to_fetch)}")
    for name, cat in to_fetch:
        print(f"  - {name} ({cat})")

    if args.dry_run:
        print("DRY RUN - nothing fetched")
        return

    kpi_conn.close()  # fetch_one/upsert below open their own connection via init_db

    fetched, failed = 0, 0
    for name, category in to_fetch:
        print(f"Fetching {name} ({category})...")
        detail = fetch_one(name, category)
        if detail.get("error"):
            print(f"  -> FAILED: {detail['error']}")
            failed += 1
            continue
        conn = init_db(args.kpi_db)
        list_row = {
            "slug": detail["slug"], "name": name, "category": category,
            "listed_date_raw": None, "issue_price_raw": None,
            "listing_price_raw": None, "listing_gain_raw": None,
            "cmp_raw": None, "cmp_gain_raw": None,
        }
        upsert(conn, list_row, detail)
        conn.close()
        fetched += 1
        time.sleep(1)  # extra courtesy gap between individual on-demand fetches

    print(f"Fetched: {fetched}, Failed: {failed}")


if __name__ == "__main__":
    main()
