"""
backfill_pruned_listed.py

One-off backfill for companies whose ipo_live_tracker row got pruned
right around their own listing date, during the same pre-fix window
that caused Dhoot's original issue -- none of these ever made it into
ipo_master_records, so they're all missing from /ipos/listed despite
having genuinely listed already:

  Dhoot Transmission Ltd    (Mainboard, listed 2026-08-17)
  Molbio Diagnostics Ltd    (Mainboard, listed 2026-08-17)
  LEAP India Ltd            (Mainboard, listed 2026-08-14)
  Technocraft Ventures Ltd  (Mainboard, listed 2026-08-14)
  Optimystix Entertainment India Ltd (SME,       listed 2026-08-14)
  LAPL Automotive Ltd       (SME,       listed 2026-08-13)
  G.V. Electricals Ltd      (SME,       listed 2026-08-12)

WHY: /ipos/listed (routers_live.py) reads listing_date straight from
ipo_master_records, nothing else. Each of these companies' ipo_live_tracker
rows were (presumably) hard-deleted before ipoji.py's fix landed, and
nothing else will ever call fetch_and_upsert() for them now (main.py's
search path resolves to an empty ipoji_partial with no tracker row --
circular, confirmed earlier for Dhoot). So each needs a direct one-off
write here. All 7 are still within the /ipos/listed 20-day cutoff and
well inside the 10-trading-day window as of today (2026-08-17).

WHAT THIS DELIBERATELY DOES NOT DO: set price_day1 / listing_day_gain_pct.
Dhoot listed today (2026-08-17) -- I only have the NSE/BSE OPEN price
(1200 / 1193.80) from news coverage, not a confirmed CLOSE. This
project standardized on close-of-day (BSE-preferred) pricing after the
open/close-basis bug that silently mixed ~576 rows -- writing today's
open into price_day1 would reintroduce that same bug for one row.
Fill price_day1/gain in a follow-up once EOD bhavcopy (or another
close-confirmed source) is available. /ipos/listed doesn't need it --
it only filters/displays on the fields set below.

BEFORE RUNNING:
  1. Confirm DB_PATH below matches config.DB_PATH in production. If
     you're running this against a local copy rather than directly on
     Render, make sure it's the same file the live app reads.
  2. I don't have db.py's schema in this session (not uploaded this
     round), so this assumes ipo_master_records has company_name,
     listing_date, issue_category, sector, subscription_total,
     open_date, close_date, allotment_date, price_band_upper,
     issue_size_cr, last_updated. If INSERT fails on a NOT NULL
     column not listed here, add it with a safe default and re-run.
  3. "sector": "Auto Ancillaries" is my best guess from Dhoot's
     business description (wiring harnesses/battery packs/sensors for
     automotive) -- confirm it matches your existing sector taxonomy
     before trusting it in the sector-feature model path.
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "ipo_database.db"  # <-- confirm this matches config.DB_PATH

# Add more dicts here for any other companies dropped in the same
# pre-fix window -- run_backfill() below handles a list, not just Dhoot.
BACKFILL_ROWS = [
    {
        "company_name": "Dhoot Transmission Ltd",
        "listing_date": "2026-08-17",
        "issue_category": "Mainboard",
        "sector": "Auto Ancillaries",  # confirm against your taxonomy
        "subscription_total": 74.21,  # NSE-sourced overall (QIB 212.92x/NII 51.93x/RII 8.12x)
        "open_date": "2026-08-10",
        "close_date": "2026-08-12",
        "allotment_date": "2026-08-13",
        "price_band_upper": 871.0,
        "issue_size_cr": 3066.89,
    },
    {
        "company_name": "Molbio Diagnostics Ltd",
        "listing_date": "2026-08-17",
        "issue_category": "Mainboard",
        "sector": "Healthcare / Diagnostics",  # confirm against your taxonomy
        "subscription_total": 70.27,  # Chittorgarh-reported final (Day 3) subscription
        "open_date": "2026-08-10",
        "close_date": "2026-08-12",
        "allotment_date": "2026-08-13",
        "price_band_upper": 807.0,
        "issue_size_cr": 939.70,
    },
    {
        "company_name": "LEAP India Ltd",
        "listing_date": "2026-08-14",
        "issue_category": "Mainboard",
        "sector": "Logistics / Supply Chain",  # confirm against your taxonomy
        "subscription_total": 8.82,  # final (Day 3) overall subscription
        "open_date": "2026-08-07",
        "close_date": "2026-08-11",
        "allotment_date": "2026-08-12",
        "price_band_upper": 159.0,
        "issue_size_cr": 2480.0,
    },
    {
        "company_name": "Technocraft Ventures Ltd",
        "listing_date": "2026-08-14",
        "issue_category": "Mainboard",
        "sector": "Infrastructure / EPC",  # confirm against your taxonomy
        "subscription_total": 38.69,  # final overall subscription
        "open_date": "2026-08-07",
        "close_date": "2026-08-11",
        "allotment_date": "2026-08-12",
        "price_band_upper": 212.0,
        "issue_size_cr": 251.88,
    },
    {
        "company_name": "Optimystix Entertainment India Ltd",
        "listing_date": "2026-08-14",
        "issue_category": "SME",
        "sector": "Media & Entertainment",  # confirm against your taxonomy
        "subscription_total": 1.91,  # final overall subscription
        "open_date": "2026-08-07",
        "close_date": "2026-08-11",
        "allotment_date": "2026-08-12",
        "price_band_upper": 175.0,
        "issue_size_cr": 108.50,
    },
    {
        "company_name": "LAPL Automotive Ltd",
        "listing_date": "2026-08-13",
        "issue_category": "SME",
        "sector": "Auto Ancillaries",  # confirm against your taxonomy
        "subscription_total": 309.37,  # final overall subscription
        "open_date": "2026-08-06",
        "close_date": "2026-08-10",
        "allotment_date": "2026-08-11",
        "price_band_upper": 94.0,
        "issue_size_cr": 32.40,
    },
    {
        "company_name": "G.V. Electricals Ltd",
        "listing_date": "2026-08-12",
        "issue_category": "SME",
        "sector": "Power Infrastructure / Electricals",  # confirm against your taxonomy
        "subscription_total": 169.26,  # final (Day 6) overall subscription
        "open_date": "2026-07-31",
        "close_date": "2026-08-07",
        "allotment_date": "2026-08-10",
        "price_band_upper": 130.0,
        "issue_size_cr": 42.25,
    },
]


def backfill_company(conn: sqlite3.Connection, data: dict, updated_at: str) -> None:
    company_name = data["company_name"]
    existing = conn.execute(
        "SELECT company_name FROM ipo_master_records WHERE company_name = ?",
        (company_name,),
    ).fetchone()

    if existing is None:
        # Loose fallback match in case the row exists under slightly
        # different casing/spacing (e.g. "Dhoot Transmission Limited").
        like_key = company_name.split()[0] + "%" + company_name.split()[1] + "%"
        existing = conn.execute(
            "SELECT company_name FROM ipo_master_records WHERE company_name LIKE ?",
            (like_key,),
        ).fetchone()
        if existing is not None:
            print(
                f"  Found existing row under a different name: {existing[0]!r} -- updating that row."
            )
            company_name = existing[0]

    fields = {k: v for k, v in data.items() if k != "company_name"}
    fields["last_updated"] = updated_at

    if existing is not None:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = ?",
            list(fields.values()) + [company_name],
        )
        print(f"  Updated existing row for {company_name!r}.")
    else:
        cols = ["company_name"] + list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO ipo_master_records ({', '.join(cols)}) VALUES ({placeholders})",
            [company_name] + list(fields.values()),
        )
        print(f"  Inserted new row for {company_name!r}.")


def run_backfill():
    conn = sqlite3.connect(DB_PATH)
    updated_at = datetime.now(timezone.utc).isoformat()
    try:
        for data in BACKFILL_ROWS:
            print(f"Backfilling {data['company_name']}...")
            backfill_company(conn, data, updated_at)
        conn.commit()

        for data in BACKFILL_ROWS:
            row = conn.execute(
                "SELECT company_name, listing_date, issue_category, sector, "
                "subscription_total, gmp_percent, price_day1 "
                "FROM ipo_master_records WHERE company_name LIKE ?",
                (
                    f"%{data['company_name'].split()[0]}%{data['company_name'].split()[1]}%",
                ),
            ).fetchone()
            print(
                "Verify:",
                dict(
                    zip(
                        [
                            "company_name",
                            "listing_date",
                            "issue_category",
                            "sector",
                            "subscription_total",
                            "gmp_percent",
                            "price_day1",
                        ],
                        row,
                    )
                )
                if row
                else "NOT FOUND -- check for an insert error above.",
            )
    finally:
        conn.close()


if __name__ == "__main__":
    run_backfill()
