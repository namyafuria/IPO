"""
write_back_symbols.py
Reads reviewed CSV, writes nse_symbol / bse_script_code into ipo_master_records.
Dry-run by default — pass --commit to actually write.
"""

import argparse
import csv
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DB_PATH = "ipo_database.db"

REQUIRED_COLS = {"company_name", "nse_symbol", "bse_script_code"}


def load_reviewed_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing cols: {missing}")
        for r in reader:
            if not r["company_name"].strip():
                continue
            if not (r["nse_symbol"].strip() or r["bse_script_code"].strip()):
                continue  # skip rows w/ nothing to write
            rows.append(r)
    return rows


def write_back(rows, commit=False):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")
    cur = conn.cursor()

    written, skipped_no_match, skipped_multi_match = 0, 0, 0

    for r in rows:
        name = r["company_name"].strip()
        nse = r["nse_symbol"].strip() or None
        bse = r["bse_script_code"].strip() or None

        cur.execute(
            "SELECT company_name FROM ipo_master_records WHERE company_name = ?",
            (name,),
        )
        matches = cur.fetchall()

        if len(matches) == 0:
            log.warning(f"NO DB MATCH: {name}")
            skipped_no_match += 1
            continue
        if len(matches) > 1:
            log.warning(f"MULTI DB MATCH ({len(matches)} rows), skipped: {name}")
            skipped_multi_match += 1
            continue

        log.info(f"{'WRITE' if commit else 'DRY-RUN'}: {name} -> nse={nse} bse={bse}")

        if commit:
            cur.execute(
                "UPDATE ipo_master_records SET nse_symbol = ?, bse_script_code = ? WHERE company_name = ?",
                (nse, bse, name),
            )
            written += 1

    if commit:
        conn.commit()
    conn.close()

    log.info(
        f"\nSummary: written={written if commit else len(rows) - skipped_no_match - skipped_multi_match} "
        f"(dry-run={not commit}), no_db_match={skipped_no_match}, multi_db_match={skipped_multi_match}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument(
        "--commit", action="store_true", help="actually write to DB (default: dry-run)"
    )
    args = ap.parse_args()

    rows = load_reviewed_csv(args.csv_path)
    write_back(rows, commit=args.commit)
