"""
nse_bse_symbol_match.py

Dry-run matcher: reads local NSE + BSE (mainboard) + BSE-SME master CSVs,
normalizes company names, matches against ipo_master_records rows missing
nse_symbol/bse_script_code, corroborates via listing_date, and LOGS
proposed matches to a CSV for manual review. Does NOT write to the DB.

Wire into the real scheduler only after a human has reviewed the proposed
matches CSV this script produces.

--- CONFIRM BEFORE RUNNING ---
1. Column names below (NSE_COLS / BSE_MAIN_COLS / BSE_SME_COLS) assume the
   headers as seen in the screenshots. Re-check against the real
   downloaded files — header casing/spacing can differ.
2. ipo_master_records column names assumed: company_name, listing_date,
   issue_category (values like 'Mainboard'/'SME'), nse_symbol,
   bse_script_code. Confirm against schemas.py's IPO_COLUMNS — adjust
   COL_* constants below if names differ.
3. This imports get_connection from app.db — run from
   ipo-tool/backend (so that import resolves), or adjust the import path.
"""

import csv
import re
import sys
import argparse
import difflib
from datetime import datetime

# ---- adjust if your schema uses different column names ----
COL_COMPANY_NAME = "company_name"
COL_LISTING_DATE = "listing_date"
COL_ISSUE_CATEGORY = "issue_category"  # 'Mainboard' / 'SME' expected
COL_NSE_SYMBOL = "nse_symbol"
COL_BSE_SCRIP = "bse_script_code"

# suffixes stripped during normalization (order matters: longest first)
STRIP_SUFFIXES = [
    "LIMITED",
    "LTD.",
    "LTD",
    "PRIVATE",
    "PVT.",
    "PVT",
    "INDIA",
    "(INDIA)",
    "COMPANY",
    "CO.",
    "CO",
]

LISTING_DATE_TOLERANCE_DAYS = 3  # corroboration slack for date mismatches


def strip_parenthetical(name: str) -> tuple[str, str]:
    """Split 'Groww (Billionbrains Garage Ventures Ltd)' into
    ('Groww', 'Billionbrains Garage Ventures Ltd') so both can be tried
    as match candidates. Second value is '' if no parens present."""
    m = re.search(r"\(([^)]+)\)", name)
    inner = m.group(1).strip() if m else ""
    outer = re.sub(r"\([^)]*\)", " ", name)
    outer = re.sub(r"\s+", " ", outer).strip()
    return outer, inner


def normalize_name(name: str) -> str:
    """Upper-case, strip punctuation, drop common corporate suffixes.
    '&' becomes the word AND first (not just dropped) so 'X & Y' and
    'X and Y' normalize the same — NSE/BSE and DB spell this differently
    row to row."""
    if not name:
        return ""
    n = name.upper()
    n = n.replace("&", " AND ")
    n = re.sub(r"[.,()]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    tokens = n.split(" ")
    # strip trailing suffix tokens repeatedly (handles "X Y LTD INDIA")
    changed = True
    while changed and tokens:
        changed = False
        for suf in STRIP_SUFFIXES:
            suf_tokens = suf.replace(".", "").split(" ")
            if tokens[-len(suf_tokens) :] == suf_tokens:
                tokens = tokens[: -len(suf_tokens)]
                changed = True
                break
    return " ".join(tokens).strip()


def _normkey(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().upper())


def name_prefix_match(a: str, b: str) -> bool:
    """True if the shorter normalized name's tokens are a leading prefix
    of the longer one's tokens (DB often stores a short/display name
    while NSE/BSE carry the full legal name — e.g. 'Ecos (India)' vs
    'ECOS INDIA MOBILITY HOSPITALITY'). Exact-equality alone misses these.
    Also true when a == b (full match still works)."""
    if not a or not b:
        return False
    ta, tb = a.split(), b.split()
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not shorter:
        return False
    return longer[: len(shorter)] == shorter


def _squash(s: str) -> str:
    return s.replace(" ", "")


def name_matches(a: str, b: str) -> bool:
    """Token-prefix match first; falls back to squashed (space-removed)
    prefix match to catch spacing/concatenation variants across sources
    — e.g. NSE 'PHYSICSWALLAH' (one word) vs DB 'PHYSICS WALLAH' (two),
    or 'R K SWAMY' vs 'RKSWAMY'. Corroboration (date / dual BSE field)
    still required downstream before accepting — this only widens what
    counts as a name candidate, doesn't accept on name alone."""
    if name_prefix_match(a, b):
        return True
    sa, sb = _squash(a), _squash(b)
    if not sa or not sb:
        return False
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(shorter) < 4:
        return False  # too short to mean anything — avoids false floods like "I" matching every I-starting name
    return longer.startswith(shorter)


def any_prefix_match(candidates: list[str], target: str) -> bool:
    return any(name_matches(c, target) for c in candidates)


def _build_header_map(fieldnames, wanted: dict) -> dict:
    """wanted: {internal_name: expected_header_label}. Matches headers
    case/whitespace-insensitively so trailing spaces or casing diffs in
    the real CSV don't silently produce empty columns."""
    norm_to_real = {_normkey(h): h for h in fieldnames}
    resolved = {}
    for internal, expected in wanted.items():
        real = norm_to_real.get(_normkey(expected))
        resolved[internal] = real  # may be None -> caller should warn
    return resolved


def load_nse(path: str) -> list[dict]:
    """NSE EQUITY_L.csv — SYMBOL, NAME OF COMPANY, DATE OF LISTING, ISIN NUMBER"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        hmap = _build_header_map(
            reader.fieldnames,
            {
                "symbol": "SYMBOL",
                "name": "NAME OF COMPANY",
                "listing_date": "DATE OF LISTING",
                "isin": "ISIN NUMBER",
            },
        )
        missing = [k for k, v in hmap.items() if v is None]
        if missing:
            print(
                f"WARNING load_nse: could not find column(s) {missing} "
                f"in headers {reader.fieldnames} — those fields will be blank."
            )
        for r in reader:
            name = r.get(hmap["name"], "") if hmap["name"] else ""
            rows.append(
                {
                    "symbol": (
                        r.get(hmap["symbol"], "") if hmap["symbol"] else ""
                    ).strip(),
                    "name": name.strip(),
                    "listing_date": (
                        r.get(hmap["listing_date"], "") if hmap["listing_date"] else ""
                    ).strip(),
                    "isin": (r.get(hmap["isin"], "") if hmap["isin"] else "").strip(),
                    "norm_name": normalize_name(name),
                    "source": "NSE",
                }
            )
    return rows


def load_bse(path: str, source_label: str) -> list[dict]:
    """
    BSE mainboard (list_scrips) or BSE-SME (bsesme.com List of Scrips).
    Cols seen: Security/Scrip Code, Issuer Name, Security/Scrip Id,
    Security/Scrip Name, Status, ISIN No.
    Uses whichever of Issuer Name / Security Name is present per row —
    keeps both normalized so either can match.
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        hmap = _build_header_map(
            reader.fieldnames,
            {
                "code": "Security Code",
                "code_sme": "Scrip Code",
                "issuer": "Issuer Name",
                "security_name": "Security Name",
                "security_name_sme": "Scrip Name",
                "status": "Status",
                "isin": "ISIN No",
            },
        )
        for r in reader:
            code = (
                (r.get(hmap["code"]) if hmap["code"] else None)
                or (r.get(hmap["code_sme"]) if hmap["code_sme"] else None)
                or ""
            )
            issuer = (r.get(hmap["issuer"]) if hmap["issuer"] else "") or ""
            sec_name = (
                (r.get(hmap["security_name"]) if hmap["security_name"] else None)
                or (
                    r.get(hmap["security_name_sme"])
                    if hmap["security_name_sme"]
                    else None
                )
                or ""
            )
            status = (r.get(hmap["status"], "") if hmap["status"] else "").strip()
            isin = (r.get(hmap["isin"], "") if hmap["isin"] else "").strip()
            if status and status.upper() != "ACTIVE":
                continue  # skip delisted/suspended rows
            rows.append(
                {
                    "scrip_code": code.strip(),
                    "issuer_name": issuer.strip(),
                    "security_name": sec_name.strip(),
                    "isin": isin,
                    "norm_issuer": normalize_name(issuer),
                    "norm_security": normalize_name(sec_name),
                    "source": source_label,
                }
            )
    return rows


def parse_date(s: str):
    if not s:
        return None
    s = s.strip()
    # strip a trailing time component if present (e.g. '2024-11-13 00:00:00',
    # '2024-11-13T00:00:00') — DB columns declared as TIMESTAMP/DATETIME
    # commonly store this even when the value is really just a date.
    # Match only a real ISO time suffix (T or space followed by digits),
    # not any literal 'T' — month abbreviations like OCT/SEPT contain one.
    m = re.match(r"^(.*?)[T ]\d{1,2}:\d{2}", s)
    if m:
        s = m.group(1)
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def dates_close(a, b, tolerance_days=LISTING_DATE_TOLERANCE_DAYS) -> bool:
    da, db_ = (
        parse_date(a) if isinstance(a, str) else a,
        parse_date(b) if isinstance(b, str) else b,
    )
    if not da or not db_:
        return False
    return abs((da - db_).days) <= tolerance_days


def fetch_unmatched_records(conn):
    """Rows missing BOTH identifiers — the only ones worth matching.
    ORDER BY rowid so DB-side pull order is fixed (removes one more
    source of run-to-run order-dependence)."""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT rowid, {COL_COMPANY_NAME}, {COL_LISTING_DATE}, {COL_ISSUE_CATEGORY}
        FROM ipo_master_records
        WHERE ({COL_NSE_SYMBOL} IS NULL OR {COL_NSE_SYMBOL} = '')
          AND ({COL_BSE_SCRIP} IS NULL OR {COL_BSE_SCRIP} = '')
        ORDER BY rowid
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fuzzy_suggestions(
    db_name_norm: str, all_candidates: list, top_n: int = 2, min_ratio: float = 0.82
):
    """
    Best-effort 'did you mean' suggestions for names that failed exact/
    prefix/squash matching — e.g. DB typos ('Lmited' vs 'Limited') or
    genuinely different wording. NEVER auto-accepted, never corroborated
    against date — purely a manual-review hint written to its own file.
    all_candidates: list of (source_tag, norm_name, display_name, code_or_symbol)
    """
    scored = []
    for source_tag, norm_name, display_name, code in all_candidates:
        if not norm_name:
            continue
        ratio = difflib.SequenceMatcher(None, db_name_norm, norm_name).ratio()
        if ratio >= min_ratio:
            scored.append((ratio, source_tag, display_name, code))
    scored.sort(key=lambda x: -x[0])
    return scored[:top_n]


def match_company(
    db_row: dict,
    nse_rows: list[dict],
    bse_main_rows: list[dict],
    bse_sme_rows: list[dict],
):
    """
    Returns (result_dict_or_None, ambiguous_flag, near_misses_list).
    near_misses_list holds diagnostic strings for name-matched-but-date-
    rejected candidates — helps find whether unresolved companies are
    failing on name (real gap) or on date tolerance (fixable threshold/
    field issue) without guessing blind.
    Collects ALL corroborated candidates per source before deciding —
    picking "first row in file" is order-dependent (CSV re-download/
    resave order isn't stable), which caused run-to-run flip on dup
    normalized names. Now: >1 candidate = ambiguous, logged, not guessed.
    """
    outer, inner = strip_parenthetical(db_row[COL_COMPANY_NAME])
    db_name_candidates = [
        normalize_name(db_row[COL_COMPANY_NAME])
    ]  # primary: same transform NSE/BSE side uses, parens kept as token
    if outer != db_row[COL_COMPANY_NAME]:
        db_name_candidates.append(normalize_name(outer))
    if inner and len(normalize_name(inner)) >= 3:
        # skip inner parenthetical candidates like "(I)" / "(P)" — 1-2
        # char abbreviations (usually short for India/Private) that
        # don't identify anything on their own and flood false matches
        # via the squash fallback below
        db_name_candidates.append(normalize_name(inner))
    db_name_candidates = list(
        dict.fromkeys(c for c in db_name_candidates if c)
    )  # dedupe, keep order
    db_listing = db_row.get(COL_LISTING_DATE)
    category = (db_row.get(COL_ISSUE_CATEGORY) or "").strip().lower()

    # Mainboard → try NSE first, then BSE mainboard.
    # SME → BSE-SME only (SME cos generally aren't NSE Emerge in this list).
    search_order = []
    if category == "sme":
        search_order = [("BSE_SME", bse_sme_rows)]
    else:
        search_order = [("NSE", nse_rows), ("BSE_MAIN", bse_main_rows)]

    all_near_misses = []
    for source_tag, rows in search_order:
        hits = []
        near_misses = []  # name matched but rejected (date mismatch etc) — for diagnostics
        for row in rows:
            if source_tag == "NSE":
                if not any_prefix_match(db_name_candidates, row["norm_name"]):
                    continue
                if dates_close(db_listing, row["listing_date"]):
                    corroborated_by = "listing_date"
                elif not row["listing_date"]:
                    corroborated_by = "name only, NSE listing_date blank (weak — flag for manual review)"
                else:
                    near_misses.append(
                        f"NSE name-matched '{row['name']}' (symbol {row['symbol']}) "
                        f"but dates disagree: db={db_listing!r} vs nse={row['listing_date']!r}"
                    )
                    continue  # both dates present but don't agree — reject, not a match
                hits.append(
                    {
                        "match_source": "NSE",
                        "nse_symbol": row["symbol"],
                        "bse_script_code": None,
                        "matched_name": row["name"],
                        "corroborated_by": corroborated_by,
                    }
                )
            else:
                if not (
                    any_prefix_match(db_name_candidates, row["norm_issuer"])
                    or any_prefix_match(db_name_candidates, row["norm_security"])
                ):
                    continue
                # BSE lists don't carry listing_date — corroborate via
                # name matching on BOTH issuer_name and security_name
                # (two independent BSE fields agreeing on distinct raw
                # strings is the "second field" signal for BSE rows).
                if row["norm_issuer"] != row["norm_security"]:
                    corroborated_by = "issuer_name+security_name agree"
                else:
                    corroborated_by = "name only (weak — flag for manual review)"
                hits.append(
                    {
                        "match_source": source_tag,
                        "nse_symbol": None,
                        "bse_script_code": row["scrip_code"],
                        "matched_name": row["issuer_name"] or row["security_name"],
                        "corroborated_by": corroborated_by,
                    }
                )

        if len(hits) == 1:
            return hits[0], False, []
        if len(hits) > 1:
            return None, True, []  # ambiguous — multiple candidate rows in this source
        # len == 0 → fall through, try next source_tag, but remember near_misses
        all_near_misses.extend(near_misses)

    return None, False, all_near_misses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nse-csv", required=True, help="path to NSE EQUITY_L.csv")
    ap.add_argument(
        "--bse-main-csv", required=True, help="path to BSE mainboard list_scrips export"
    )
    ap.add_argument(
        "--bse-sme-csv", required=True, help="path to BSE-SME list_scrips export"
    )
    ap.add_argument("--db-path", required=True, help="path to ipo_database.db")
    ap.add_argument("--out", default="proposed_symbol_matches.csv")
    args = ap.parse_args()

    import sqlite3

    conn = sqlite3.connect(args.db_path)

    nse_rows = load_nse(args.nse_csv)
    bse_main_rows = load_bse(args.bse_main_csv, "BSE_MAIN")
    bse_sme_rows = load_bse(args.bse_sme_csv, "BSE_SME")
    unmatched = fetch_unmatched_records(conn)

    print(
        f"NSE rows: {len(nse_rows)} | BSE mainboard: {len(bse_main_rows)} | "
        f"BSE SME: {len(bse_sme_rows)} | DB rows needing match: {len(unmatched)}"
    )
    if nse_rows:
        print("  NSE sample row (check name/date format look right):", nse_rows[0])
    if unmatched:
        print(
            "  DB sample row (check listing_date format matches NSE's):", unmatched[0]
        )

    proposed, unresolved, ambiguous = [], [], []
    near_miss_log = []
    for db_row in unmatched:
        result, is_ambiguous, near_misses = match_company(
            db_row, nse_rows, bse_main_rows, bse_sme_rows
        )
        if near_misses:
            for nm in near_misses:
                near_miss_log.append(
                    {"company_name": db_row[COL_COMPANY_NAME], "detail": nm}
                )
        if result:
            proposed.append(
                {
                    COL_COMPANY_NAME: db_row[COL_COMPANY_NAME],
                    "db_listing_date": db_row.get(COL_LISTING_DATE),
                    "db_category": db_row.get(COL_ISSUE_CATEGORY),
                    **result,
                }
            )
        elif is_ambiguous:
            ambiguous.append(db_row[COL_COMPANY_NAME])
        else:
            unresolved.append(db_row)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        if proposed:
            w = csv.DictWriter(f, fieldnames=list(proposed[0].keys()))
            w.writeheader()
            w.writerows(proposed)

    from collections import Counter

    src_counts = Counter(p["match_source"] for p in proposed)
    print(f"Proposed matches: {len(proposed)} → written to {args.out}")
    print(f"  breakdown by source: {dict(src_counts)}")
    print(
        f"Unresolved (no corroborated match, left for manual review): {len(unresolved)}"
    )
    if unresolved:
        with open("unresolved_companies.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company_name", "db_listing_date", "likely_reason"])
            for row in unresolved:
                name = row[COL_COMPANY_NAME]
                if not row.get(COL_LISTING_DATE):
                    reason = "not listed yet (blank listing_date) — expected, not a bug"
                elif re.search(r"\b(REIT|InvIT|Trust)\b", name, re.IGNORECASE):
                    reason = "REIT/InvIT/Trust — likely not in standard equity list, check NSE REIT/InvIT segment separately"
                else:
                    reason = (
                        "already listed but no name match found — check suffix/spelling"
                    )
                w.writerow([name, row.get(COL_LISTING_DATE), reason])
        print(
            "Unresolved list written to unresolved_companies.csv (now split by likely_reason column)"
        )

        # fuzzy 'did you mean' pass — diagnostic only, never auto-accepted
        all_candidates = (
            [("NSE", r["norm_name"], r["name"], r["symbol"]) for r in nse_rows]
            + [
                ("BSE_MAIN", r["norm_issuer"], r["issuer_name"], r["scrip_code"])
                for r in bse_main_rows
            ]
            + [
                ("BSE_MAIN", r["norm_security"], r["security_name"], r["scrip_code"])
                for r in bse_main_rows
            ]
            + [
                ("BSE_SME", r["norm_issuer"], r["issuer_name"], r["scrip_code"])
                for r in bse_sme_rows
            ]
            + [
                ("BSE_SME", r["norm_security"], r["security_name"], r["scrip_code"])
                for r in bse_sme_rows
            ]
        )
        fuzzy_rows = []
        for row in unresolved:
            if not row.get(COL_LISTING_DATE):
                continue  # not listed yet — nothing to suggest against
            if re.search(
                r"\b(REIT|InvIT|Trust)\b", row[COL_COMPANY_NAME], re.IGNORECASE
            ):
                continue  # separate known gap, not a spelling issue
            db_norm = normalize_name(row[COL_COMPANY_NAME])
            suggestions = fuzzy_suggestions(db_norm, all_candidates)
            for ratio, source_tag, display_name, code in suggestions:
                fuzzy_rows.append(
                    {
                        "company_name": row[COL_COMPANY_NAME],
                        "suggested_source": source_tag,
                        "suggested_name": display_name,
                        "suggested_code": code,
                        "similarity": f"{ratio:.2f}",
                    }
                )
        if fuzzy_rows:
            with open("fuzzy_suggestions.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "company_name",
                        "suggested_source",
                        "suggested_name",
                        "suggested_code",
                        "similarity",
                    ],
                )
                w.writeheader()
                w.writerows(fuzzy_rows)
            print(
                f"Fuzzy 'did you mean' suggestions (NOT auto-matched, manual confirm only): "
                f"{len(fuzzy_rows)} → fuzzy_suggestions.csv"
            )

    print(f"Ambiguous (multiple candidate rows, needs manual pick): {len(ambiguous)}")
    if ambiguous:
        with open("ambiguous_companies.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company_name"])
            for name in ambiguous:
                w.writerow([name])
        print("Ambiguous list written to ambiguous_companies.csv")

    print(f"Name-matched-but-date-rejected (diagnostic): {len(near_miss_log)}")
    if near_miss_log:
        with open(
            "date_mismatch_near_misses.csv", "w", newline="", encoding="utf-8"
        ) as f:
            w = csv.DictWriter(f, fieldnames=["company_name", "detail"])
            w.writeheader()
            w.writerows(near_miss_log)
        print(
            "Near-miss detail written to date_mismatch_near_misses.csv — open this first."
        )

    conn.close()


if __name__ == "__main__":
    sys.exit(main())
