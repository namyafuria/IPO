"""
One-time backfill of KPI fields (pe_ratio, roe, debt_equity + extras) for
companies in ipo_master_records that don't have them yet, via ipoguru.py.

REVISED 2026-08-28: switched from ipogyani.py to ipoguru.py (ipogyani's
listing-page discovery kept 404ing / not matching -- see project notes).
ipoguru.py resolves slugs from its own /ipo-performance + /sme-ipo-performance
listing pages, no year needed. Query now only needs company_name +
issue_category.

Dry-run by default (prints planned writes, touches nothing). Pass --write to
actually persist.

NOTE: live_fetch.fetch_and_upsert()'s _ipogyani_partial() (if it exists)
still points at the old module -- not updated here, flag separately if that
auto-fill path should also move to ipoguru.
"""

import argparse
import sqlite3
import sys

from app.fetchers.ipoguru import fetch_kpi_for_company

DB_PATH = "ipo_database.db"

KPI_COLUMNS = [
    "pe_ratio",
    "roe",
    "debt_equity",
    # extras -- only written if the columns exist on the table; see main()
    "roce",
    "ronw",
    "pat_margin",
    "ebitda_margin",
    "price_to_book",
    "eps_pre",
    "eps_post",
    "promoter_holding_pre",
    "promoter_holding_post",
    "market_cap",
]


def get_missing_kpi_companies(conn) -> list[tuple[str, str | None]]:
    """Returns (company_name, issue_category) -- ipoguru's listing pages
    aren't year-partitioned so listing_date is no longer needed to
    resolve a slug; ORDER BY still uses it even though it's not selected."""
    cur = conn.cursor()
    cur.execute("""
        SELECT company_name, issue_category
        FROM ipo_master_records
        WHERE pe_ratio IS NULL AND roe IS NULL AND debt_equity IS NULL
        ORDER BY listing_date DESC
    """)
    return cur.fetchall()


def existing_columns(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(ipo_master_records)")
    return {row[1] for row in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--write", action="store_true", help="actually persist (default: dry-run)"
    )
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cols_present = existing_columns(conn)
    writable_cols = [c for c in KPI_COLUMNS if c in cols_present]
    skipped_cols = [c for c in KPI_COLUMNS if c not in cols_present]
    if skipped_cols:
        print(
            f"NOTE: columns not on ipo_master_records yet, will be skipped: {skipped_cols}"
        )

    rows = get_missing_kpi_companies(conn)
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} companies missing KPI data\n")

    filled, no_match, no_data = 0, [], []

    for name, issue_category in rows:
        result = fetch_kpi_for_company(name, issue_category)
        if result is None:
            no_match.append(name)
            print(f"  [no slug match / fetch failed] {name}")
            continue

        present_vals = {
            k: v for k, v in result.items() if v is not None and k in writable_cols
        }
        if not present_vals:
            no_data.append(name)
            print(f"  [fetched, no usable fields] {name}")
            continue

        print(f"  [OK] {name}: {present_vals}")
        if args.write:
            set_clause = ", ".join(f"{k} = ?" for k in present_vals)
            conn.execute(
                f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = ?",
                (*present_vals.values(), name),
            )
        filled += 1

    if args.write:
        conn.commit()
        print(f"\nCommitted. {filled} companies updated.")
    else:
        print(
            f"\nDRY RUN -- {filled} would be updated. Re-run with --write to persist."
        )

    if no_match:
        print(f"\n{len(no_match)} needs manual slug / not on ipoguru: {no_match}")
    if no_data:
        print(f"\n{len(no_data)} fetched OK but had no usable KPI rows: {no_data}")

    conn.close()


if __name__ == "__main__":
    main()
