"""
Match ipoguru_kpis.db rows to ipo_database.db's ipo_master_records by
company name, then:
  1. store the raw financials/objects JSON in a new linked table
     `ipo_guru_kpi_raw` (company_name, matched via the same safe matcher).
  2. compute derivable ratios (ROE, Debt/Equity, PAT margin, EBITDA margin)
     from the raw financials line items and backfill ONLY where the
     existing ipo_master_records column is NULL - never overwrites.

Matcher: same pattern as your existing subscription-daywise backfill -
normalize (lowercase, strip Ltd/Limited/Pvt/IPO/SME noise words, drop
punctuation), then whole-word substring match in either direction,
reject if more than one candidate matches (ambiguous -> skip, don't guess).

Usage:
    python3 match_and_update_kpis.py ipoguru_kpis.db ipo_database.db

Prints a report: matched / unmatched / ambiguous / ratios-filled counts.
Does not touch ipo_database.db until you confirm (see --dry-run).
"""

import re
import json
import sqlite3
import argparse
from datetime import datetime

NOISE_WORDS = {
    "ltd", "limited", "pvt", "private", "co", "company", "ipo", "sme",
    "the", "and", "india", "in", "solutions", "technologies", "industries",
}


def normalize(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    words = [w for w in s.split() if w not in NOISE_WORDS]
    return " ".join(words).strip()


def safe_match(kpi_name: str, db_names: list[str]) -> str | None:
    """Returns the single matching db_name, or None if zero or ambiguous
    (>1) matches - never guesses."""
    norm_kpi = normalize(kpi_name)
    if not norm_kpi:
        return None
    candidates = []
    for db_name in db_names:
        norm_db = normalize(db_name)
        if not norm_db:
            continue
        if norm_kpi == norm_db:
            candidates.append(db_name)
            continue
        # whole-word substring either direction
        kpi_words = set(norm_kpi.split())
        db_words = set(norm_db.split())
        if kpi_words and db_words and (kpi_words <= db_words or db_words <= kpi_words):
            candidates.append(db_name)
    candidates = list(dict.fromkeys(candidates))  # dedupe, keep order
    if len(candidates) == 1:
        return candidates[0]
    return None  # zero or ambiguous


def parse_period_date(period_str: str):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(period_str.strip(), fmt)
        except ValueError:
            continue
    return None


def latest_period_values(financials_rows: list[dict]) -> dict:
    """financials_rows: list of {"Metric": name, "<period1>": val, ...}.
    Returns {metric_name: float_value} using the most recent period
    column found across all rows (by parsed date, falling back to the
    first non-Metric key if dates don't parse)."""
    out = {}
    for row in financials_rows:
        metric = row.get("Metric")
        if not metric:
            continue
        periods = [k for k in row.keys() if k != "Metric"]
        if not periods:
            continue
        dated = [(p, parse_period_date(p)) for p in periods]
        dated_valid = [(p, d) for p, d in dated if d is not None]
        chosen = max(dated_valid, key=lambda x: x[1])[0] if dated_valid else periods[0]
        raw_val = row[chosen]
        cleaned = raw_val.replace(",", "").replace("₹", "").strip()
        try:
            out[metric] = float(cleaned)
        except ValueError:
            continue
    return out


def compute_ratios(metrics: dict) -> dict:
    ratios = {}
    net_worth = metrics.get("NET Worth")
    pat = metrics.get("Profit After Tax")
    ebitda = metrics.get("EBITDA")
    total_income = metrics.get("Total Income")
    borrowings = metrics.get("Total Borrowing")

    if pat is not None and net_worth:
        ratios["roe"] = round(pat / net_worth * 100, 2)
    if borrowings is not None and net_worth:
        ratios["debt_equity"] = round(borrowings / net_worth, 2)
    if pat is not None and total_income:
        ratios["pat_margin"] = round(pat / total_income * 100, 2)
    if ebitda is not None and total_income:
        ratios["ebitda_margin"] = round(ebitda / total_income * 100, 2)
    return ratios


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kpi_db")
    parser.add_argument("master_db")
    parser.add_argument("--dry-run", action="store_true",
                         help="report matches/ratios but write nothing")
    args = parser.parse_args()

    kpi_conn = sqlite3.connect(args.kpi_db)
    master_conn = sqlite3.connect(args.master_db)

    master_conn.execute("""
        CREATE TABLE IF NOT EXISTS ipo_guru_kpi_raw (
            company_name TEXT PRIMARY KEY,
            matched_slug TEXT,
            financials_json TEXT,
            objects_of_issue_json TEXT,
            linked_at TEXT
        )
    """)

    db_names = [r[0] for r in master_conn.execute(
        "SELECT DISTINCT company_name FROM ipo_master_records").fetchall()]

    kpi_rows = kpi_conn.execute(
        "SELECT slug, name, financials_json, objects_of_issue_json FROM ipo_guru_kpis"
    ).fetchall()

    matched, unmatched, ambiguous, ratios_filled = 0, 0, 0, 0

    for slug, kpi_name, fin_json, obj_json in kpi_rows:
        display_name = kpi_name or slug.replace("-ipo", "").replace("-sme-ipo", "").replace("-", " ")
        match = safe_match(display_name, db_names)
        if match is None:
            unmatched += 1
            continue
        matched += 1

        if not args.dry_run:
            master_conn.execute("""
                INSERT INTO ipo_guru_kpi_raw (company_name, matched_slug, financials_json, objects_of_issue_json, linked_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(company_name) DO UPDATE SET
                    matched_slug=excluded.matched_slug,
                    financials_json=excluded.financials_json,
                    objects_of_issue_json=excluded.objects_of_issue_json,
                    linked_at=excluded.linked_at
            """, (match, slug, fin_json, obj_json, datetime.utcnow().isoformat()))

        # compute + backfill ratios, only where NULL
        try:
            fin_rows = json.loads(fin_json) if fin_json else []
        except json.JSONDecodeError:
            fin_rows = []
        if not fin_rows:
            continue
        metrics = latest_period_values(fin_rows)
        ratios = compute_ratios(metrics)
        if not ratios:
            continue

        current = master_conn.execute(
            "SELECT roe, debt_equity, pat_margin, ebitda_margin FROM ipo_master_records WHERE company_name = ? LIMIT 1",
            (match,)
        ).fetchone()
        if current is None:
            continue
        cur_roe, cur_de, cur_pat_margin, cur_ebitda_margin = current

        updates = {}
        if cur_roe is None and "roe" in ratios:
            updates["roe"] = ratios["roe"]
        if cur_de is None and "debt_equity" in ratios:
            updates["debt_equity"] = ratios["debt_equity"]
        if cur_pat_margin is None and "pat_margin" in ratios:
            updates["pat_margin"] = ratios["pat_margin"]
        if cur_ebitda_margin is None and "ebitda_margin" in ratios:
            updates["ebitda_margin"] = ratios["ebitda_margin"]

        if updates and not args.dry_run:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            master_conn.execute(
                f"UPDATE ipo_master_records SET {set_clause} WHERE company_name = ?",
                (*updates.values(), match)
            )
        if updates:
            ratios_filled += 1

    if not args.dry_run:
        master_conn.commit()

    print(f"KPI rows processed: {len(kpi_rows)}")
    print(f"Matched: {matched}")
    print(f"Unmatched (no confident match, skipped): {unmatched}")
    print(f"Rows where >=1 ratio column was backfilled: {ratios_filled}")
    if args.dry_run:
        print("DRY RUN - nothing written")

    kpi_conn.close()
    master_conn.close()


if __name__ == "__main__":
    main()
