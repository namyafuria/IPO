"""
check_proposed_matches.py

Spot-check pass over proposed_symbol_matches.csv before write-back.
Doesn't touch DB — read-only sanity check.

Flags:
1. Duplicate nse_symbol / bse_script_code assigned to 2+ different
   companies — serious bug if found, means 2 real companies got
   collapsed onto one identifier.
2. Rows where corroborated_by is a "weak" match (name only, no second
   signal) — lower confidence than listing_date-corroborated or
   dual-field-agreed rows, worth a human look before trusting.

Prints a summary + writes 2 CSVs for the flagged subsets so you don't
have to scroll the full file by eye.
"""

import csv
import sys
import argparse
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True, help="path to proposed_symbol_matches.csv")
    args = ap.parse_args()

    rows = []
    with open(args.infile, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    print(f"Total proposed rows: {len(rows)}")

    # --- 1. duplicate identifier check ---
    nse_map = defaultdict(list)
    bse_map = defaultdict(list)
    for r in rows:
        if r.get("nse_symbol"):
            nse_map[r["nse_symbol"]].append(r["company_name"])
        if r.get("bse_script_code"):
            bse_map[r["bse_script_code"]].append(r["company_name"])

    dupe_nse = {k: v for k, v in nse_map.items() if len(v) > 1}
    dupe_bse = {k: v for k, v in bse_map.items() if len(v) > 1}

    if dupe_nse or dupe_bse:
        print(f"!! DUPLICATE nse_symbol assigned to multiple companies: {len(dupe_nse)}")
        for sym, names in dupe_nse.items():
            print(f"   {sym}: {names}")
        print(f"!! DUPLICATE bse_script_code assigned to multiple companies: {len(dupe_bse)}")
        for code, names in dupe_bse.items():
            print(f"   {code}: {names}")
    else:
        print("No duplicate identifiers found across proposed rows — good.")

    # --- 2. weak-confidence rows ---
    weak_rows = [r for r in rows if "weak" in (r.get("corroborated_by") or "").lower()]
    print(f"Weak-confidence rows (name only, no second signal): {len(weak_rows)}")
    if weak_rows:
        with open("weak_confidence_matches.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(weak_rows[0].keys()))
            w.writeheader()
            w.writerows(weak_rows)
        print("  -> written to weak_confidence_matches.csv, review these before write-back")

    strong_rows = [r for r in rows if r not in weak_rows]
    print(f"Strong-confidence rows (listing_date match or dual-field agree): {len(strong_rows)}")

    if dupe_nse or dupe_bse:
        with open("duplicate_identifier_matches.csv", "w", newline="", encoding="utf-8") as f:
            all_dupe_names = set()
            for names in list(dupe_nse.values()) + list(dupe_bse.values()):
                all_dupe_names.update(names)
            dupe_rows = [r for r in rows if r["company_name"] in all_dupe_names]
            w = csv.DictWriter(f, fieldnames=list(dupe_rows[0].keys()))
            w.writeheader()
            w.writerows(dupe_rows)
        print("  -> written to duplicate_identifier_matches.csv, FIX THESE FIRST before write-back")


if __name__ == "__main__":
    sys.exit(main())
