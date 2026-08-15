"""
app/services/ipoji.py

Live IPO Ji integration for the running backend.

This module owns two things that used to be split across two standalone
scripts + a shared helper:

  1. Fetch/parse logic  (moved in from ipoji_common.py, unchanged) -- given a
     slug, get the details/GMP/subscription HTML and turn it into dicts.
  2. Save logic  (new, Step 2) -- given those dicts, upsert them into
     gmp_trend / subscription_daywise / ipo_live_tracker using the keys Step 1
     put in place: UNIQUE(company_name, gmp_date), UNIQUE(company_name,
     day_number), and ipo_live_tracker's company_name PRIMARY KEY.

What's intentionally NOT reused from the old scripts
------------------------------------------------------
step4_live_poller_ipoji.py appended a new timestamped row to a CSV on every
poll ("keep the whole history of every check"). We don't do that here --
Step 1's UNIQUE constraints mean a poll UPSERTS the day's row in place, so
gmp_trend/subscription_daywise end up holding "latest known value for that
day," refined every time the hour job runs until the day is over, not one
row per poll. That's a deliberate change from the old script's behaviour,
not an oversight -- flagging it here in case it matters for anything
downstream that expected raw poll-by-poll history.

FLAGGED ASSUMPTIONS -- verify against one real live page before trusting
production writes from this module (see inline comments at each site):
  1. company_name is DERIVED from the slug (ipoji.com's parsed pages never
     expose a plain company-name field) and then resolved against
     ipo_master_records via db.find_company()'s existing fuzzy match. If
     that resolution fails, we fall back to the slug-derived name and flag
     it -- we do NOT silently guess a wrong canonical name.
  3. subscription_daywise.snapshot_date uses today's date (poll time), since
     parse_subscription()'s "as_on" field is a bidding-day label ("Day 1"),
     not a calendar date -- there's no other date source on that page.
  4. gmp_trend.day_tag is inferred here (not scraped) by comparing gmp_date
     against the IPO's listing_date/allotment_date from the details page.
     .employee has no source on this site's tables and is left NULL.

RESOLVED (were flagged assumptions, now confirmed/fixed):
  2. subscription table column mapping -- CONFIRMED via two screenshots
     (Shiprocket, Behari Lal Engineering) cross-checked against a 32-row
     live test poll: ipoji's OPEN/live page renders "Total" via JS, which
     requests-based scraping can't execute, so raw columns are offset --
     nii_or_bhni cell actually holds "<NII> <bHNI> <sHNI>" (or just <NII>
     alone when no HNI split is shown), shni column is really Retail,
     retail column is really Total, total column is a duplicate/garbage.
     Fixed in poll_and_save_open_ipos()'s subscription loop below.
  5. subscription_daywise.nii is NOT the sum of s_nii + b_nii -- that was
     wrong (bHNI/sHNI are reservation-weighted, not summed); nii is now
     read directly from the confirmed real NII-total token instead.
  6. (new, found alongside the above) _to_float() didn't strip the
     trailing x/X that every subscription multiple carries ("1.28x"),
     so every subscription field was silently coming back None even
     before the column-remap bug -- fixed below.
"""

import re
import sqlite3
import time
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

from .. import config
from ..db import get_connection, find_company

# ---------------------------------------------------------------------------
# Section 1 — fetch/parse (moved from ipoji_common.py, logic unchanged)
# ---------------------------------------------------------------------------

BASE = "https://www.ipoji.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}
DELAY_SECONDS = 1.5
MAX_RETRIES = 3

SLUG_RE = re.compile(r"/ipo/([a-z0-9\-]+-ipo)\b", re.IGNORECASE)
EXCLUDE_SLUGS = {"current-ipo", "upcoming-ipo", "listed-ipo"}
CURRENT_PAGES = ["/ipo/current-ipo", "/sme-ipo/current-ipo"]


def fetch(url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 522, 503):
                time.sleep(DELAY_SECONDS * attempt * 2)
                continue
            return None
        except requests.RequestException:
            time.sleep(DELAY_SECONDS * attempt * 2)
    return None


def extract_slugs(html: str) -> set[str]:
    return {s.lower() for s in SLUG_RE.findall(html)} - EXCLUDE_SLUGS


def clean_num(text: str) -> str:
    if text is None:
        return ""
    t = text.replace("\u2013", "-").replace("\u2014", "-").strip()
    t = t.replace("\u20b9", "").strip()
    return t


def find_table_by_headers(soup: BeautifulSoup, must_contain: list[str]):
    for table in soup.find_all("table"):
        header_cells = table.find_all(["th", "td"], limit=15)
        header_text = " ".join(c.get_text(" ", strip=True).lower() for c in header_cells)
        if all(term.lower() in header_text for term in must_contain):
            return table
    return None


def table_to_rows(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [clean_num(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return rows


def parse_details_page(slug: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text_blocks = [t.get_text(" ", strip=True) for t in soup.find_all(["div", "li", "p"])]
    full_text = "\n".join(text_blocks)

    record = {"slug": slug}

    label_patterns = {
        "price_band": r"Price band\s*[\n:]*\s*₹?([\d,]+-[\d,]+)",
        "min_investment": r"Minimum Investment\s*[\n:]*\s*₹?([\d,]+)",
        "issue_size": r"Issue size\s*[\n:]*\s*₹?([\d,]+\s*Cr)",
        "lot_size": r"Lot size\s*[\n:]*\s*(\d+)",
        "allotment_date": r"Allotment Date\s*[\n:]*\s*([A-Za-z]+ \d{1,2},? \d{4})",
        "listing_date": r"Listing(?: Date)?\s*[\n:]*\s*([A-Za-z]+ \d{1,2},? \d{4})",
        "listing_at": r"Listing At\s*[\n:]*\s*([A-Za-z, ]+?)(?:\n|IPO)",
        "face_value": r"Face value\s*[\n:]*\s*₹?([\d.]+)",
        "fresh_issue": r"Fresh issue\s*[\n:]*\s*([\d,]+ shares)",
        "ofs": r"Offer for sale \(OFS\)\s*[\n:]*\s*([\d,]+ shares)",
        "retail_portion_pct": r"Retail Portion\s*[\n:]*\s*([\d.]+%)",
        "registrar": r"Registrar\s*[\n:]*\s*([A-Za-z0-9 .&,]+?)(?:\n|Lead manager)",
        "current_gmp": r"GMP Today:\s*₹?(-?\d+)",
    }
    for field, pattern in label_patterns.items():
        m = re.search(pattern, full_text)
        record[field] = m.group(1).strip() if m else None

    record["ipo_type"] = "SME" if "SME" in full_text[:2000] else "Mainboard"

    m = re.search(r"IPO Dates\s*\n?\s*([A-Za-z]+ \d{1,2}, \d{4})\s*[–\-]\s*([A-Za-z]+ \d{1,2}, \d{4})", full_text)
    if m:
        record["open_date"], record["close_date"] = m.group(1), m.group(2)

    return record


def parse_gmp_daily(slug: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = find_table_by_headers(soup, ["date", "gmp"])
    if not table:
        return []
    rows = table_to_rows(table)
    if not rows:
        return []
    header, data_rows = rows[0], rows[1:]
    out = []
    for r in data_rows:
        if len(r) < 4:
            continue
        out.append({
            "slug": slug,
            "date": r[0],
            "gmp": r[1],
            "change": r[2] if len(r) > 2 else None,
            "gmp_pct": r[3] if len(r) > 3 else None,
            "indicative_listing_price": r[4] if len(r) > 4 else None,
        })
    return out


def parse_gmp_intraday(slug: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = find_table_by_headers(soup, ["date & time", "gmp"])
    if not table:
        return []
    rows = table_to_rows(table)
    if not rows:
        return []
    header, data_rows = rows[0], rows[1:]
    out = []
    for r in data_rows:
        if len(r) < 4:
            continue
        out.append({
            "slug": slug,
            "datetime": r[0],
            "gmp": r[1],
            "change": r[2] if len(r) > 2 else None,
            "gmp_pct": r[3] if len(r) > 3 else None,
            "indicative_price": r[4] if len(r) > 4 else None,
        })
    return out


def parse_subscription(slug: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = find_table_by_headers(soup, ["qib", "total"])
    if not table:
        return []
    rows = table_to_rows(table)
    if not rows:
        return []
    header, data_rows = rows[0], rows[1:]
    out = []
    for r in data_rows:
        if len(r) < 3:
            continue
        is_day_row = bool(re.match(r"Day\s*\d+", r[0], re.IGNORECASE))
        out.append({
            "slug": slug,
            "as_on": r[0],
            "is_bidding_day": is_day_row,
            "qib": r[1] if len(r) > 1 else None,
            "nii_or_bhni": r[2] if len(r) > 2 else None,
            "shni": r[3] if len(r) > 3 else None,
            "retail": r[4] if len(r) > 4 else None,
            "total": r[-1],
        })
    return out


def fetch_and_parse_ipo(slug: str) -> dict:
    result = {"details": None, "gmp_intraday": [], "gmp_daily": [], "subscription": [], "fetch_errors": []}

    detail_html = fetch(f"{BASE}/ipo/{slug}")
    time.sleep(DELAY_SECONDS)
    if detail_html:
        result["details"] = parse_details_page(slug, detail_html)
        result["gmp_intraday"] = parse_gmp_intraday(slug, detail_html)
    else:
        result["fetch_errors"].append("details")

    gmp_daily_html = fetch(f"{BASE}/ipo-gmp/{slug}")
    time.sleep(DELAY_SECONDS)
    if gmp_daily_html:
        result["gmp_daily"] = parse_gmp_daily(slug, gmp_daily_html)
    else:
        result["fetch_errors"].append("gmp_daily")

    sub_html = fetch(f"{BASE}/ipo-subscription/{slug}")
    time.sleep(DELAY_SECONDS)
    if sub_html:
        result["subscription"] = parse_subscription(slug, sub_html)
    else:
        result["fetch_errors"].append("subscription")

    return result


def discover_open_slugs() -> set[str]:
    """Moved from step4_live_poller_ipoji.py -- what the hourly job polls."""
    slugs = set()
    for page in CURRENT_PAGES:
        html = fetch(BASE + page)
        if not html:
            print(f"  [open-check] failed to fetch: {page}")
            continue
        slugs.update(extract_slugs(html))
    return slugs


# ---------------------------------------------------------------------------
# Section 2 — cleaning helpers (new: the old scripts wrote raw strings to CSV,
# the DB columns are REAL/TEXT-typed and need real conversion)
# ---------------------------------------------------------------------------

def _to_float(s: str | None) -> float | None:
    """'21.48%' -> 21.48, '1,49,13,000' -> 14913000.0, '-5' -> -5.0, '-' -> None,
    '1.28x' -> 1.28 (subscription multiples always carry a trailing x/X --
    without stripping it, float() raises and every subscription field comes
    back None; confirmed bug found alongside the column-remap fix below)."""
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("%", "").replace("₹", "")
    if s in ("", "-", "—", "NA", "N/A"):
        return None
    s = s.rstrip("xX").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_MONTH_DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})")
_DMY_DASH_RE = re.compile(r"(\d{1,2})[-/]([A-Za-z]{3,})[-/](\d{4})")
_MONTHS = {m.lower()[:3]: i for i, m in enumerate(
    ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]) if m}


def _to_iso_date(s: str | None) -> str | None:
    """Best-effort parse of ipoji's date strings into 'YYYY-MM-DD'.
    Handles 'Aug 7, 2026' and '7-Aug-2026' / '7/Aug/2026' forms, which cover
    both the details-page dates and the gmp_daily table's date column as
    observed in this project's earlier sessions. Returns None (does not
    raise) on anything unrecognized -- callers must handle None and skip
    the row rather than write a bad date, since gmp_date/day fields are
    part of the UNIQUE key.
    """
    if not s:
        return None
    s = s.strip()

    m = _MONTH_DATE_RE.search(s)
    if m:
        mon, day, year = m.groups()
        mon_num = _MONTHS.get(mon.lower()[:3])
        if mon_num:
            return f"{year}-{mon_num:02d}-{int(day):02d}"

    m = _DMY_DASH_RE.search(s)
    if m:
        day, mon, year = m.groups()
        mon_num = _MONTHS.get(mon.lower()[:3])
        if mon_num:
            return f"{year}-{mon_num:02d}-{int(day):02d}"

    return None


_SLUG_SUFFIX_RE = re.compile(r"-ipo$", re.IGNORECASE)


def _slug_to_display_name(slug: str) -> str:
    """'anawil-wire-engineering-ipo' -> 'Anawil Wire Engineering'. A rough
    fallback only -- resolve_company_name() below tries to map this onto
    the DB's real canonical name (e.g. 'Anawil Wire & Engineering Ltd.')
    before using this raw form."""
    base = _SLUG_SUFFIX_RE.sub("", slug)
    return " ".join(w.capitalize() for w in base.split("-"))


def resolve_company_name(slug: str) -> tuple[str, bool]:
    """Returns (name_to_use, matched_existing). Tries to resolve the
    slug-derived name against ipo_master_records via db.py's existing fuzzy
    matcher (same cutoff=0.6 convention used by the InvestorGain loader and
    db.find_company() itself) so ipoji-sourced rows land under the same
    company identity as everything else in the DB. Falls back to the raw
    slug-derived name -- and reports matched_existing=False -- rather than
    guessing, so callers can log/flag unresolved companies instead of
    silently mis-keying them."""
    guess = _slug_to_display_name(slug)
    record, exact = find_company(guess)
    if record is not None:
        return record.company_name, True
    return guess, False


def _parse_price_band_upper(price_band: str | None) -> float | None:
    """'265-270' -> 270.0"""
    if not price_band:
        return None
    parts = re.split(r"[-–]", price_band.replace(",", ""))
    if not parts:
        return None
    return _to_float(parts[-1])


# ---------------------------------------------------------------------------
# Section 3 — upserts (Step 2's actual new logic)
# ---------------------------------------------------------------------------

def upsert_gmp_trend(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    gmp_date: str,
    ipo_price: float | None = None,
    gmp_value: float | None = None,
    subscription_at_snapshot: float | None = None,
    est_listing_price: float | None = None,
    est_profit_pct: float | None = None,
    day_tag: str | None = None,
    last_updated: str | None = None,
    source: str = "ipoji",
) -> None:
    conn.execute(
        """
        INSERT INTO gmp_trend
            (company_name, gmp_date, ipo_price, gmp_value, subscription_at_snapshot,
             est_listing_price, est_profit_pct, day_tag, last_updated, source)
        VALUES
            (:company_name, :gmp_date, :ipo_price, :gmp_value, :subscription_at_snapshot,
             :est_listing_price, :est_profit_pct, :day_tag, :last_updated, :source)
        ON CONFLICT(company_name, gmp_date) DO UPDATE SET
            ipo_price = excluded.ipo_price,
            gmp_value = excluded.gmp_value,
            subscription_at_snapshot = excluded.subscription_at_snapshot,
            est_listing_price = excluded.est_listing_price,
            est_profit_pct = excluded.est_profit_pct,
            -- keep an existing day_tag (e.g. a manually-set 'Listing') unless
            -- this poll actually has a non-null one to offer
            day_tag = COALESCE(excluded.day_tag, gmp_trend.day_tag),
            last_updated = excluded.last_updated,
            source = excluded.source
        """,
        {
            "company_name": company_name,
            "gmp_date": gmp_date,
            "ipo_price": ipo_price,
            "gmp_value": gmp_value,
            "subscription_at_snapshot": subscription_at_snapshot,
            "est_listing_price": est_listing_price,
            "est_profit_pct": est_profit_pct,
            "day_tag": day_tag,
            "last_updated": last_updated,
            "source": source,
        },
    )


def upsert_subscription_daywise(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    day_number: int,
    snapshot_date: str,
    qib: float | None = None,
    nii: float | None = None,
    s_nii: float | None = None,
    b_nii: float | None = None,
    rii: float | None = None,
    overall: float | None = None,
    employee: float | None = None,
    source: str = "ipoji",
) -> None:
    conn.execute(
        """
        INSERT INTO subscription_daywise
            (company_name, day_number, snapshot_date, qib, nii, s_nii, b_nii,
             rii, overall, employee, source)
        VALUES
            (:company_name, :day_number, :snapshot_date, :qib, :nii, :s_nii, :b_nii,
             :rii, :overall, :employee, :source)
        ON CONFLICT(company_name, day_number) DO UPDATE SET
            snapshot_date = excluded.snapshot_date,
            qib = excluded.qib,
            nii = excluded.nii,
            s_nii = excluded.s_nii,
            b_nii = excluded.b_nii,
            rii = excluded.rii,
            overall = excluded.overall,
            employee = COALESCE(excluded.employee, subscription_daywise.employee),
            source = excluded.source
        """,
        {
            "company_name": company_name,
            "day_number": day_number,
            "snapshot_date": snapshot_date,
            "qib": qib,
            "nii": nii,
            "s_nii": s_nii,
            "b_nii": b_nii,
            "rii": rii,
            "overall": overall,
            "employee": employee,
            "source": source,
        },
    )


def upsert_live_tracker(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    issue_category: str | None = None,
    sector: str | None = None,
    status: str | None = None,
    open_date: str | None = None,
    close_date: str | None = None,
    price_band_upper: float | None = None,
    issue_size_cr: float | None = None,
    current_subscription_total: float | None = None,
    current_subscription_qib: float | None = None,
    current_subscription_hni: float | None = None,
    current_subscription_rii: float | None = None,
    current_gmp_percent: float | None = None,
    as_of: str | None = None,
) -> None:
    """Single row per company, fully overwritten -- matches the table's
    stated design (no history kept here; gmp_trend/subscription_daywise
    are the history)."""
    conn.execute(
        """
        INSERT INTO ipo_live_tracker
            (company_name, issue_category, sector, status, open_date, close_date,
             price_band_upper, issue_size_cr, current_subscription_total,
             current_subscription_qib, current_subscription_hni,
             current_subscription_rii, current_gmp_percent, as_of)
        VALUES
            (:company_name, :issue_category, :sector, :status, :open_date, :close_date,
             :price_band_upper, :issue_size_cr, :current_subscription_total,
             :current_subscription_qib, :current_subscription_hni,
             :current_subscription_rii, :current_gmp_percent, :as_of)
        ON CONFLICT(company_name) DO UPDATE SET
            issue_category = excluded.issue_category,
            sector = excluded.sector,
            status = excluded.status,
            open_date = excluded.open_date,
            close_date = excluded.close_date,
            price_band_upper = excluded.price_band_upper,
            issue_size_cr = excluded.issue_size_cr,
            current_subscription_total = excluded.current_subscription_total,
            current_subscription_qib = excluded.current_subscription_qib,
            current_subscription_hni = excluded.current_subscription_hni,
            current_subscription_rii = excluded.current_subscription_rii,
            current_gmp_percent = excluded.current_gmp_percent,
            as_of = excluded.as_of
        """,
        {
            "company_name": company_name,
            "issue_category": issue_category,
            "sector": sector,
            "status": status,
            "open_date": open_date,
            "close_date": close_date,
            "price_band_upper": price_band_upper,
            "issue_size_cr": issue_size_cr,
            "current_subscription_total": current_subscription_total,
            "current_subscription_qib": current_subscription_qib,
            "current_subscription_hni": current_subscription_hni,
            "current_subscription_rii": current_subscription_rii,
            "current_gmp_percent": current_gmp_percent,
            "as_of": as_of,
        },
    )


def remove_from_live_tracker(conn: sqlite3.Connection, company_name: str) -> None:
    """Called once an IPO closes (its closing snapshot has been saved) --
    ipo_live_tracker is meant to hold only currently-open IPOs, per the
    /ipos/open use case in Step 6."""
    conn.execute("DELETE FROM ipo_live_tracker WHERE company_name = ?", (company_name,))


# ---------------------------------------------------------------------------
# Section 4 — orchestration: one full poll cycle, saved to the DB
# ---------------------------------------------------------------------------

def poll_and_save_open_ipos() -> dict:
    """The function Step 3's scheduler calls once an hour (and that the
    manual /api/sync route can call on demand). Discovers currently-open
    IPOs, fetches+parses each, and upserts into all three tables.

    Returns a summary dict for logging/the sync endpoint's response --
    NOT the raw scraped data.
    """
    summary = {
        "polled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "open_slugs_found": 0,
        "companies_saved": [],
        "unresolved_company_names": [],  # slug-derived names with no DB match -- flag for review
        "fetch_errors": [],
    }

    open_slugs = discover_open_slugs()
    summary["open_slugs_found"] = len(open_slugs)

    conn = get_connection()
    try:
        for slug in sorted(open_slugs):
            result = fetch_and_parse_ipo(slug)
            if result["fetch_errors"]:
                summary["fetch_errors"].append({"slug": slug, "pages": result["fetch_errors"]})

            company_name, matched = resolve_company_name(slug)
            if not matched:
                summary["unresolved_company_names"].append({"slug": slug, "guessed_name": company_name})

            details = result["details"] or {}
            today_iso = date.today().isoformat()
            listing_date_iso = _to_iso_date(details.get("listing_date"))
            allotment_date_iso = _to_iso_date(details.get("allotment_date"))
            ipo_price = _parse_price_band_upper(details.get("price_band"))

            # --- gmp_trend: one upsert per day-row from the GMP history table ---
            latest_gmp_pct = None
            for row in result["gmp_daily"]:
                gmp_date_iso = _to_iso_date(row.get("date"))
                if not gmp_date_iso:
                    continue  # can't upsert without the UNIQUE key's date half
                day_tag = None
                if listing_date_iso and gmp_date_iso == listing_date_iso:
                    day_tag = "Listing"
                elif allotment_date_iso and gmp_date_iso == allotment_date_iso:
                    day_tag = "Allotment"

                gmp_pct = _to_float(row.get("gmp_pct"))
                if gmp_date_iso == today_iso:
                    latest_gmp_pct = gmp_pct

                upsert_gmp_trend(
                    conn,
                    company_name=company_name,
                    gmp_date=gmp_date_iso,
                    ipo_price=ipo_price,
                    gmp_value=_to_float(row.get("gmp")),
                    subscription_at_snapshot=None,  # see module docstring, assumption 3-adjacent gap
                    est_listing_price=_to_float(row.get("indicative_listing_price")),
                    est_profit_pct=gmp_pct,
                    day_tag=day_tag,
                    last_updated=summary["polled_at"],
                    source="ipoji",
                )

            # --- subscription_daywise: one upsert per real bidding-day row ---
            latest_sub_total = latest_sub_qib = latest_sub_hni = latest_sub_rii = None
            for row in result["subscription"]:
                if not row.get("is_bidding_day"):
                    continue
                m = re.search(r"Day\s*(\d+)", row["as_on"], re.IGNORECASE)
                if not m:
                    continue
                day_number = int(m.group(1))

                # FIXED (was ASSUMPTION 2, now confirmed via two screenshots --
                # Shiprocket + Behari Lal Engineering -- cross-checked against
                # a 32-row live test poll): ipoji's OPEN/live subscription page
                # renders "Total" via JS, which requests-based scraping can't
                # execute, so the raw scraped columns are offset from their
                # names:
                #   nii_or_bhni cell holds "<NII-total> <bHNI> <sHNI>" (3
                #     space-separated numbers), or just "<NII-total>" alone
                #     when that IPO's page shows no bHNI/sHNI split;
                #   shni column   -> real Retail multiple
                #   retail column -> real Total multiple
                #   total column  -> duplicate/garbage, discarded
                # Also: nii is NOT b_nii + s_nii (old bug) -- bHNI/sHNI are
                # reservation-weighted, not summed, so nii is read directly
                # from the first token instead of recomputed.
                nii_tokens = (row.get("nii_or_bhni") or "").split()
                if len(nii_tokens) >= 3:
                    nii = _to_float(nii_tokens[0])
                    b_nii = _to_float(nii_tokens[1])
                    s_nii = _to_float(nii_tokens[2])
                elif len(nii_tokens) == 1:
                    nii = _to_float(nii_tokens[0])
                    b_nii = s_nii = None
                else:
                    nii = b_nii = s_nii = None
                rii = _to_float(row.get("shni"))
                overall = _to_float(row.get("retail"))
                # row.get("total") is the duplicate/garbage cell -- intentionally unused.
                qib = _to_float(row.get("qib"))

                latest_sub_total, latest_sub_qib, latest_sub_rii = overall, qib, rii
                latest_sub_hni = nii

                upsert_subscription_daywise(
                    conn,
                    company_name=company_name,
                    day_number=day_number,
                    snapshot_date=today_iso,  # ASSUMPTION 3 -- see module docstring
                    qib=qib,
                    nii=nii,
                    s_nii=s_nii,
                    b_nii=b_nii,
                    rii=rii,
                    overall=overall,
                    employee=None,
                    source="ipoji",
                )

            # --- ipo_live_tracker: single overwritten row for this company ---
            upsert_live_tracker(
                conn,
                company_name=company_name,
                issue_category=details.get("ipo_type"),
                sector=None,  # not exposed by parse_details_page; leave for a later enrichment pass
                status="open",
                open_date=_to_iso_date(details.get("open_date")) or details.get("open_date"),
                close_date=_to_iso_date(details.get("close_date")) or details.get("close_date"),
                price_band_upper=ipo_price,
                issue_size_cr=_to_float((details.get("issue_size") or "").replace("Cr", "")),
                current_subscription_total=latest_sub_total,
                current_subscription_qib=latest_sub_qib,
                current_subscription_hni=latest_sub_hni,
                current_subscription_rii=latest_sub_rii,
                current_gmp_percent=latest_gmp_pct or _to_float(details.get("current_gmp")),
                as_of=summary["polled_at"],
            )

            summary["companies_saved"].append(company_name)

        # Anything in ipo_live_tracker that's no longer in the open set today
        # has closed since the last poll -- drop it (Step 6's /ipos/open reads
        # this table directly, so it must only ever hold currently-open IPOs).
        cur = conn.execute("SELECT company_name FROM ipo_live_tracker")
        tracked = {r["company_name"] for r in cur.fetchall()}
        still_open = set(summary["companies_saved"])
        for stale_name in tracked - still_open:
            remove_from_live_tracker(conn, stale_name)

        conn.commit()
    finally:
        conn.close()

    return summary


if __name__ == "__main__":
    import json
    print(json.dumps(poll_and_save_open_ipos(), indent=2))
