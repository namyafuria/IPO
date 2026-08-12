"""
gmp_sync.py -- server-side wrapper around both GMP scrapers, callable from
a FastAPI route or the existing scheduler (see main.py's new
POST /api/sync/gmp route, and the scheduler.py integration note at the
bottom of this file).

Two real sources, kept separate rather than merged into one script, since
they have very different shapes:

  ipogyani.com (ipogyani_scraper.py logic, inlined here)
    -- server-rendered <table>s, confirmed working (2026-08-12). Gives
    full day-wise history per live IPO in one page: price, gmp, est
    listing price/%, subscription. Fast: one page to discover live IPOs,
    then one page per company.

  ipowatch.in (ipowatch_gmp_scraper.py logic, adapted here)
    -- NOT confirmed working end-to-end (no internet in any sandbox this
    project has used). Discovery is a full-site sitemap crawl, then one
    page per company, each with unstructured "Grey market premium as on
    <date> at Rs.X-Y" bullet lines going back potentially years. Only
    gives a GMP value per date -- no subscription/est-listing-price
    directly, though est_listing_price/est_profit_pct can be *derived*
    here from the matched company's price_band_upper once a company_name
    match is found. Company identity only comes from the URL slug, not a
    clean name, so this path needs slug->name derivation + strict_match,
    same pattern as load_subscription_bulk.py's slug_to_name().

IMPORTANT -- ipowatch's discovery step (sitemap crawl of the whole site)
can be slow and hit potentially hundreds of pages. On a serverless host
(Vercel), an HTTP request that runs this synchronously WILL likely hit
the platform's execution time limit before finishing a full run. Two
ways to handle that, pick based on your host:
  - Long-running host (Render/VM/RUN_SCHEDULER=1 box): fine to call
    run_gmp_sync() directly from a cron-triggered endpoint, even
    uncapped -- it just takes a few minutes.
  - Serverless: always pass a `limit` on ipowatch (e.g. limit=15) per
    invocation, and call the endpoint repeatedly (e.g. every 10 minutes
    via Vercel Cron) rather than expecting one call to finish the whole
    site. ipogyani's live-IPO-only scope is small enough to not need this.

Uses the same strict whole-word-substring company-name matcher as
load_subscription_bulk.py / load_investorgain_batch.py throughout --
not difflib -- for the documented false-positive reasons.

--- FIX LOG (2026-08-12) ---
1. config.DB_PATH didn't exist -- added to config.py separately.
2. This file inlined ipogyani_scraper.py's fetch/parse logic but dropped
   its backfill_gmp_percent_strict() step, so ipo_master_records.gmp_percent
   (the field the UI actually reads) stayed NULL even after a successful
   sync. Restored below as _backfill_master_from_gmp_trend(), called at the
   end of run_gmp_sync() after both sources have committed their gmp_trend
   rows. Now diffed against the real ipogyani_scraper.py.
   backfill_gmp_percent_strict() (recovered separately) -- confirms the
   matcher and "latest row per company" logic are the same, but ONE real
   behavioral difference: the original only wrote gmp_percent where it was
   currently NULL (`if cur[0] is None: UPDATE`); _backfill_master_from_
   gmp_trend() below unconditionally overwrites it every run. Left as
   overwrite-always here on purpose -- this runs repeatedly as a "live"
   sync, and NULL-only would freeze gmp_percent at its first-ever value
   instead of tracking the current GMP. Flag if NULL-only was actually
   intentional (e.g. because gmp_percent is meant to be authoritatively
   owned by the investorgain snapshot import and this is only a one-time
   gap-filler) and this should be reverted.
   Doesn't touch price_band_upper/pe_ratio/dates (those come from the
   investorgain subscription-snapshot CSV via load_subscription_bulk.py,
   not from gmp_trend) -- separate, still-open backfill gap.
3. Added _ipogyani_fetch_live_status() (scrapes /live-ipo) as the discovery
   source for scheduler.py's sync_active_ipos(), replacing
   ipoguru.fetch_active_ipos() -- see that function's own docstring for
   why (IPOGURU_API_KEY was never set) and for the two parsing bugs fixed
   while building it.
4. _LIVE_CARD_RE's status group didn't recognize the bare "Listing Day"
   variant (confirmed via Ardee Industries production text: "...2026Listing
   DayMainboardPrice Band..."), so that card (and any other on its listing
   day) silently failed to match at all. Added "Listing Day" to the status
   alternation and normalize it to "Closed" (same as the dated "Listing on
   <date>" form). Also speculatively added "Allotment Day" by the same
   pattern, since it's the natural counterpart -- NOT yet confirmed against
   a real card. If the unmatched-card diagnostic log still shows cards
   failing on an allotment-day card, that's the first thing to check.
5. Added _ipogyani_fetch_subscription_categories() -- subscription_qib/
   subscription_hni/subscription_rii were declared in schemas.py but never
   actually populated by any fetch path, which is why they held stale/
   arithmetically-inconsistent values next to a live subscription_total.
   Source is ipogyani's per-company /ipo/{slug}/subscription page -- see
   that function's own docstring for the confirmed caveat that this page's
   own total doesn't agree with /live-ipo's, so only the category
   multiples are used, never a total.
"""
import logging
import re
import sqlite3
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger("ipo_tool.gmp_sync")

HEADERS_IPOGYANI = {"User-Agent": "ipo-analyser-personal-project/1.0 (research use)"}
HEADERS_IPOWATCH = {"User-Agent": "Mozilla/5.0 (research script)"}

IPOGYANI_BASE = "https://ipogyani.com"
IPOGYANI_LIVE_URL = f"{IPOGYANI_BASE}/ipo-gmp-today"
IPOGYANI_LIVE_STATUS_URL = f"{IPOGYANI_BASE}/live-ipo"

IPOWATCH_BASE = "https://ipowatch.in"
IPOWATCH_SITEMAP_CANDIDATES = ["/sitemap.xml", "/sitemap_index.xml", "/post-sitemap.xml", "/sitemap-1.xml"]
IPOWATCH_GMP_LINE_RE = re.compile(
    r"Grey market premium as on\s+(today|\d{1,2}-\d{1,2}-\d{4})"
    r"\s+at\s+Rs\.?\s*(\d+)(?:\s*-\s*(\d+))?",
    re.I,
)

SLEEP_BETWEEN_REQUESTS = 1.5

# --- shared strict matcher (identical to load_subscription_bulk.py) ---
_SUFFIXES_RE = re.compile(r"\b(limited|ltd|private|pvt|company|co|the|formerly|india)\b", re.IGNORECASE)


def _normalize_name(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = _SUFFIXES_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strict_match(name, db_names):
    n = _normalize_name(name)
    toks = n.split()
    if not toks:
        return None
    significant = len(toks) >= 2 or (len(toks) == 1 and len(toks[0]) >= 6)
    if not significant:
        return None
    pattern = r"\b" + re.escape(n) + r"\b"
    candidates = set()
    for db_name in db_names:
        dbn = _normalize_name(db_name)
        if dbn and (re.search(pattern, dbn) or re.search(r"\b" + re.escape(dbn) + r"\b", n)):
            candidates.add(db_name)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _upsert_gmp_trend(c, rows):
    """rows: list of tuples matching gmp_trend's column order."""
    c.executemany(
        """
        INSERT INTO gmp_trend
            (company_name, gmp_date, ipo_price, gmp_value, subscription_at_snapshot,
             est_listing_price, est_profit_pct, day_tag, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_name, gmp_date) DO UPDATE SET
            ipo_price=excluded.ipo_price,
            gmp_value=excluded.gmp_value,
            subscription_at_snapshot=excluded.subscription_at_snapshot,
            est_listing_price=excluded.est_listing_price,
            est_profit_pct=excluded.est_profit_pct,
            day_tag=excluded.day_tag,
            last_updated=excluded.last_updated
        """,
        rows,
    )


def _backfill_master_from_gmp_trend(c):
    """Restores the gmp_percent push that ipogyani_scraper.py's
    backfill_gmp_percent_strict() used to do, which got dropped when its
    logic was inlined into sync_ipogyani()/sync_ipowatch() below.

    For every company in gmp_trend, take its most recent row (by gmp_date)
    and push est_profit_pct into ipo_master_records.gmp_percent for the
    strict-matched company. Only ever overwrites gmp_percent -- does not
    touch price_band_upper/pe_ratio/close_date/allotment_date/listing_date,
    since those come from a different source (investorgain snapshot CSV,
    see load_subscription_bulk.py) that this sync path never had.

    Returns the number of ipo_master_records rows updated.
    """
    db_names = [r[0] for r in c.execute("SELECT company_name FROM ipo_master_records").fetchall()]

    latest_per_company = {}
    for company_name, gmp_date, est_profit_pct in c.execute(
        """
        SELECT company_name, gmp_date, est_profit_pct
        FROM gmp_trend
        WHERE est_profit_pct IS NOT NULL
        """
    ).fetchall():
        prev = latest_per_company.get(company_name)
        if prev is None or gmp_date > prev[0]:
            latest_per_company[company_name] = (gmp_date, est_profit_pct)

    updated = 0
    for company_name, (gmp_date, est_profit_pct) in latest_per_company.items():
        db_name = strict_match(company_name, db_names)
        if db_name is None:
            continue
        c.execute(
            "UPDATE ipo_master_records SET gmp_percent = ? WHERE company_name = ?",
            (est_profit_pct, db_name),
        )
        updated += c.rowcount if c.rowcount and c.rowcount > 0 else 0
    return updated


# ---------------------------------------------------------------------------
# ipogyani.com
# ---------------------------------------------------------------------------
def _ipogyani_fetch_live():
    r = requests.get(IPOGYANI_LIVE_URL, headers=HEADERS_IPOGYANI, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("No <table> found on ipogyani.com/ipo-gmp-today -- page structure may have changed.")

    out = []
    for tr in table.find_all("tr"):
        link = tr.find("a", href=re.compile(r"^/ipo/[a-z0-9-]+$"))
        if link is None:
            continue
        slug = re.search(r"^/ipo/([a-z0-9-]+)$", link["href"]).group(1)
        name = link.get_text(strip=True)
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        row_text = " ".join(cells)
        band_match = re.search(r"Rs\s*([\d,]+)(?:\s*-\s*([\d,]+))?\s*$", row_text)
        band_high = float(band_match.group(2).replace(",", "")) if (band_match and band_match.group(2)) else (
            float(band_match.group(1).replace(",", "")) if band_match else None
        )
        out.append({"company_name": name, "slug": slug, "price_band_high": band_high})
    return out


_IPOGYANI_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_IPOGYANI_DATE_RE = re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.IGNORECASE)


def _ipogyani_parse_date(text, today=None):
    today = today or date.today()
    m = _IPOGYANI_DATE_RE.search(text)
    if not m:
        return None
    day, mon_str = int(m.group(1)), m.group(2).lower()
    month = _IPOGYANI_MONTHS[mon_str]
    year = today.year
    if month - today.month > 2:
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _ipogyani_fetch_history(slug, price_band_high):
    url = f"{IPOGYANI_BASE}/ipo/{slug}/gmp"
    r = requests.get(url, headers=HEADERS_IPOGYANI, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 4:
            continue
        date_str, gmp_str, pct_str, listing_str = cells[:4]
        gmp_date = _ipogyani_parse_date(date_str)
        if gmp_date is None:
            continue
        gmp_m = re.search(r"([+-]?[\d,]+(?:\.\d+)?)", gmp_str)
        pct_m = re.search(r"([+-]?[\d,]+(?:\.\d+)?)", pct_str)
        listing_m = re.search(r"([+-]?[\d,]+(?:\.\d+)?)", listing_str)
        out.append((
            gmp_date,
            price_band_high,
            float(gmp_m.group(1).replace(",", "")) if gmp_m else None,
            None,  # subscription_at_snapshot -- not on this page, see ipogyani_scraper.py docstring
            float(listing_m.group(1).replace(",", "")) if listing_m else None,
            float(pct_m.group(1).replace(",", "")) if pct_m else None,
            None,  # day_tag
            None,  # last_updated
        ))
    return out


# ---------------------------------------------------------------------------
# ipogyani.com/live-ipo -- FIX LOG (2026-08-12), part 2
#
# Replaces ipoguru.fetch_active_ipos() as the discovery source for
# scheduler.py's sync_active_ipos(), since IPOGURU_API_KEY was never set
# on Render (confirmed from the env var list) and that's been failing
# silently ever since. /ipo-gmp-today above has no date columns at all --
# just name/GMP/price band -- so it can't drive the "LIVE IPOS" tab
# (find_live_and_recent_companies() needs open_date/close_date). This page
# has those, plus status, subscription, category, and allotment/listing
# dates once bidding closes.
#
# Each IPO is one <a href="/ipo/{slug}"> card. Read with NO separator
# (a[...].get_text(strip=True), same call the rest of this module makes),
# fields that sit in separate child elements come out concatenated with no
# whitespace between them wherever the source HTML has no whitespace text
# node there, e.g. (confirmed from real production output, 2026-08-12):
#   "Dhoot Transmission10 Aug - 12 Aug, 2026Last DayMainboardPrice BandRs
#    829-871Lot:17Subscription74.15xDay 3Issue SizeRs 3,067CrFresh..."
# CORRECTED (2026-08-12, second pass): an earlier version of this pattern
# also expected the company name to appear twice back-to-back ("{name}
# logo{name}") -- that turned out to be an artifact of how a different
# fetch tool had converted the logo <img>'s alt text into visible text
# during testing. BeautifulSoup's get_text() does NOT include img alt
# attributes at all, so real production text has the name only once. The
# real, confirmed-from-production issue is: status/category/label words
# butt directly against digits or each other ("2026Last Day",
# "MainboardPrice", "Lot:17", "3,067Cr") with no space, so \b-anchored or
# literal-single-space regex silently fails to match every card -- fixed
# by matching the whole card as one ordered sequence with \s* (zero-or-
# more, not exactly-one) between fields.
# ---------------------------------------------------------------------------
_LIVE_CARD_RE = re.compile(
    r"^(?P<name>.+?)"
    r"(?P<open_day>\d{1,2})\s+(?P<open_mon>[A-Za-z]{3})\s*-\s*(?P<close_day>\d{1,2})\s+(?P<close_mon>[A-Za-z]{3}),\s*(?P<year>\d{4})"
    r"(?P<status>Open|Last Day|Closed|Listing Day|Listing on \d{1,2} [A-Za-z]{3}|Allotment Day|Allotment on \d{1,2} [A-Za-z]{3}|Upcoming)"
    r"(?P<category>Mainboard|SME)"
    r"\s*Price\s*Band\s*Rs\s*(?P<band_low>[\d,]+)\s*-\s*(?P<band_high>[\d,]+)"
    r"Lot:\s*(?P<lot>\d+)"
    r"Subscription(?P<sub>[\d.]+x|-)"
    r"(?P<day_tag>Day \d+|Final|Not open)"
    r"Issue\s*Size\s*Rs\s*(?P<issue_size>[\d,.]+)\s*Cr"
    r".*?GMP:(?P<gmp_pct>[+-]?[\d.]+)%"
    r".*?AI Gain(?P<ai_pct>[+-]?[\d.]+)%",
    re.DOTALL,
)


def _card_date(day, mon, year):
    month = _IPOGYANI_MONTHS[mon.lower()]
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def _ipogyani_fetch_live_status():
    """One dict per card on /live-ipo: company_name, slug, category,
    status (Open/Last Day/Closed/Upcoming), open_date, close_date,
    allotment_date, listing_date, subscription_total, price_band_low/high,
    lot_size, issue_size_cr, gmp_percent, ai_pred_pct.

    "Listing on <date>" and "Allotment on <date>" cards are past their
    close_date (bidding is over), so status is normalized to "Closed" for
    those with the extra date captured separately -- callers that just
    want "still open for bidding" can check status == "Open"/"Last Day"
    without also special-casing the on-<date> phrasing.
    """
    r = requests.get(IPOGYANI_LIVE_STATUS_URL, headers=HEADERS_IPOGYANI, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    all_cards = soup.find_all("a", href=re.compile(r"^/ipo/[a-z0-9-]+$"))
    out = []
    n_unmatched = 0
    sample_unmatched_text = None
    for a in all_cards:
        slug_m = re.search(r"^/ipo/([a-z0-9-]+)$", a["href"])
        if slug_m is None:
            continue
        text = a.get_text(strip=True)
        m = _LIVE_CARD_RE.search(text)
        if m is None:
            n_unmatched += 1
            if sample_unmatched_text is None:
                sample_unmatched_text = text[:200]
            continue

        year = m.group("year")
        status_raw = m.group("status")
        allotment_date = listing_date = None
        am = re.match(r"Allotment on (\d{1,2}) ([A-Za-z]{3})", status_raw)
        lm = re.match(r"Listing on (\d{1,2}) ([A-Za-z]{3})", status_raw)
        if am:
            allotment_date = _card_date(am.group(1), am.group(2), year)
            status = "Closed"
        elif lm:
            listing_date = _card_date(lm.group(1), lm.group(2), year)
            status = "Closed"
        elif status_raw in ("Listing Day", "Allotment Day"):
            # Bare variant with no date attached (confirmed in production for
            # "Listing Day" -- Ardee Industries, 2026-08-12; "Allotment Day"
            # is the same pattern by inference, NOT yet confirmed against a
            # real card -- watch the unmatched-card diagnostic log for it).
            # Same meaning as the dated "X on <date>" forms -- bidding is
            # closed -- but no date digits are present to parse, so
            # allotment_date/listing_date stay None here.
            status = "Closed"
        else:
            status = status_raw

        sub_raw = m.group("sub")
        out.append({
            "company_name": m.group("name"),
            "slug": slug_m.group(1),
            "category": m.group("category"),
            "status": status,
            "open_date": _card_date(m.group("open_day"), m.group("open_mon"), year),
            "close_date": _card_date(m.group("close_day"), m.group("close_mon"), year),
            "allotment_date": allotment_date,
            "listing_date": listing_date,
            "subscription_total": float(sub_raw.rstrip("x")) if sub_raw != "-" else None,
            "price_band_low": float(m.group("band_low").replace(",", "")),
            "price_band_high": float(m.group("band_high").replace(",", "")),
            "lot_size": int(m.group("lot")),
            "issue_size_cr": float(m.group("issue_size").replace(",", "")),
            "gmp_percent": float(m.group("gmp_pct")),
            "ai_pred_pct": float(m.group("ai_pct")),
        })

    # DIAGNOSTIC (2026-08-12) -- this page is currently returning 0 matches
    # in production for reasons not yet confirmed (works when fetched
    # directly, but Render's plain requests.get() sees something
    # different -- possibly this page renders its cards client-side via
    # JS, unlike /ipo-gmp-today which is confirmed server-rendered).
    # These log lines exist to tell the three failure modes apart without
    # more guessing:
    if not all_cards:
        logger.warning(
            "ipogyani /live-ipo: found 0 <a href=\"/ipo/...\"> elements at all "
            "(response length %d chars) -- page may be JS-rendered client-side "
            "rather than server-rendered, so a plain requests.get() sees an "
            "empty shell. First 300 chars of response: %r",
            len(r.text), r.text[:300],
        )
    elif not out:
        logger.warning(
            "ipogyani /live-ipo: found %d card(s) but 0 matched the expected "
            "format -- page structure has likely changed. Sample unmatched "
            "card text: %r", len(all_cards), sample_unmatched_text,
        )
    elif n_unmatched:
        logger.warning(
            "ipogyani /live-ipo: matched %d of %d cards -- %d unmatched, "
            "sample: %r", len(out), len(all_cards), n_unmatched, sample_unmatched_text,
        )
    return out


# ---------------------------------------------------------------------------
# ipogyani.com/ipo/{slug}/subscription -- retail/NII/QIB category breakdown
#
# FIX (2026-08-12): schemas.py has always declared subscription_qib/
# subscription_hni/subscription_rii, but no fetch path in this project ever
# populated them -- they were left holding whatever an old one-time import
# put there, which visibly stopped matching subscription_total once that
# field started getting refreshed live via _ipogyani_fetch_live_status()
# (e.g. Dhoot Transmission showing HNI 2.47x/RII 0.76x next to a live
# total of 74.2x -- arithmetically impossible together).
#
# CONFIRMED (2026-08-12) via direct fetch of both a mid-bidding company
# (Dhoot Transmission) and a closed one (Ardee Industries): ipogyani has
# THREE pages that each report a "live" total for the same company, and
# they do not agree -- Dhoot showed 74.21x on /ipo-subscription's inline
# breakdown vs 15.96x on its own /ipo/{slug}/subscription page, fetched
# within minutes of each other. The per-company page below is simply on a
# slower/different refresh cadence than /live-ipo (the source
# subscription_total already comes from). There is no way to make the
# category breakdown agree with subscription_total using this source --
# only /ipo-subscription's own per-card expand-on-click has the freshest
# numbers, and that's client-rendered, invisible to a plain GET.
#
# So: this function returns ONLY the three category multiples, never a
# total -- callers must not use it to overwrite subscription_total, and
# should expect the category figures to occasionally lag the total shown
# elsewhere. That's a real limitation of the source, not a bug here.
#
# Extracted from the page's "Quick Answer" summary sentence (a fixed SEO
# template -- "...subscribed X.XXx overall, with the retail category at
# X.XXx, NII at X.XXx and QIB at X.XXx.") rather than any guessed CSS
# class/div structure, since this project already got burned once this
# session assuming a fetch tool's rendering matches raw requests.get()
# structural markup -- a stable prose sentence is far more robust to that
# gap than a speculative selector would be.
# ---------------------------------------------------------------------------
_SUB_CATEGORY_RE = re.compile(
    r"subscribed\s+([\d,.]+)x\s+overall,?\s*with\s+the\s+retail\s+category\s+at\s+([\d,.]+)x,?\s*"
    r"NII\s+at\s+([\d,.]+)x\s+and\s+QIB\s+at\s+([\d,.]+)x",
    re.IGNORECASE,
)


def _ipogyani_fetch_subscription_categories(slug):
    """Retail/NII/QIB subscription multiples for one company, from
    ipogyani.com/ipo/{slug}/subscription. Returns
    {"subscription_rii": float, "subscription_hni": float,
    "subscription_qib": float}, or {} if the page has no bids yet, the
    summary sentence isn't found (format change), or the request fails --
    treated the same as any other source with nothing to say.
    """
    url = f"{IPOGYANI_BASE}/ipo/{slug}/subscription"
    try:
        r = requests.get(url, headers=HEADERS_IPOGYANI, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning("ipogyani subscription-category fetch failed for slug %r: %s", slug, e)
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = _SUB_CATEGORY_RE.search(text)
    if m is None:
        logger.warning(
            "ipogyani /ipo/%s/subscription: category summary sentence not found -- "
            "sample text: %r", slug, text[:300],
        )
        return {}

    _total_str, retail_str, nii_str, qib_str = m.groups()
    return {
        "subscription_rii": float(retail_str.replace(",", "")),
        "subscription_hni": float(nii_str.replace(",", "")),
        "subscription_qib": float(qib_str.replace(",", "")),
    }


def sync_ipogyani(conn):
    c = conn.cursor()
    live = _ipogyani_fetch_live()
    n_rows = 0
    for entry in live:
        if not entry["slug"]:
            continue
        history = _ipogyani_fetch_history(entry["slug"], entry["price_band_high"])
        if not history:
            continue
        _upsert_gmp_trend(c, [(entry["company_name"],) + h for h in history])
        n_rows += len(history)
        time.sleep(SLEEP_BETWEEN_REQUESTS)
    conn.commit()
    return {"source": "ipogyani", "companies": len(live), "rows_upserted": n_rows}


# ---------------------------------------------------------------------------
# ipowatch.in
# ---------------------------------------------------------------------------
def _ipowatch_discover_urls():
    urls = set()
    for path in IPOWATCH_SITEMAP_CANDIDATES:
        try:
            resp = requests.get(IPOWATCH_BASE + path, headers=HEADERS_IPOWATCH, timeout=30)
            if resp.status_code != 200:
                continue
        except Exception:
            continue
        soup = BeautifulSoup(resp.text, "xml")
        sub_sitemaps = [loc.text for loc in soup.find_all("loc") if loc.text.endswith(".xml")]
        page_urls = [loc.text for loc in soup.find_all("loc") if "grey-market-premium" in loc.text]
        urls.update(page_urls)
        for sm_url in sub_sitemaps:
            try:
                r2 = requests.get(sm_url, headers=HEADERS_IPOWATCH, timeout=30)
                if r2.status_code != 200:
                    continue
                soup2 = BeautifulSoup(r2.text, "xml")
                urls.update(loc.text for loc in soup2.find_all("loc") if "grey-market-premium" in loc.text)
                time.sleep(0.5)
            except Exception:
                continue
        if urls:
            break

    if not urls:
        try:
            resp = requests.get(IPOWATCH_BASE + "/ipo-gmp/", headers=HEADERS_IPOWATCH, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "grey-market-premium" in a["href"]:
                    href = a["href"]
                    urls.add(href if href.startswith("http") else IPOWATCH_BASE + href)
        except Exception:
            pass

    return sorted(urls)


def _ipowatch_slug_to_name(slug):
    """'xyz-ipo-grey-market-premium-xyz-ipo-gmp-today' -> 'Xyz'. ipowatch
    pages have no clean company-name field, only this URL slug -- same
    kind of derivation as load_subscription_bulk.py's slug_to_name(), but
    stripping this site's specific suffix pattern instead."""
    s = re.sub(r"-ipo-grey-market-premium-.*$", "", slug, flags=re.IGNORECASE)
    s = s.replace("-", " ").replace("_", " ")
    return " ".join(w.capitalize() for w in s.split())


def _ipowatch_parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for block in soup.find_all(["li", "p"]):
        text = block.get_text(" ", strip=True)
        m = IPOWATCH_GMP_LINE_RE.search(text)
        if m:
            date_str, gmp_lo, gmp_hi = m.groups()
            rows.append((date_str, float(gmp_lo), float(gmp_hi) if gmp_hi else float(gmp_lo)))
    return rows


def sync_ipowatch(conn, limit=None):
    c = conn.cursor()
    db_names = [r[0] for r in c.execute("SELECT company_name, price_band_upper FROM ipo_master_records").fetchall()]
    price_by_name = dict(c.execute("SELECT company_name, price_band_upper FROM ipo_master_records").fetchall())

    urls = _ipowatch_discover_urls()
    if limit:
        urls = urls[:limit]

    n_pages, n_rows, unmatched = 0, 0, []
    for url in urls:
        slug_m = re.search(r"ipowatch\.in/([^/]+)/?$", url)
        slug = slug_m.group(1) if slug_m else url
        try:
            resp = requests.get(url, headers=HEADERS_IPOWATCH, timeout=30)
            resp.raise_for_status()
        except Exception:
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        raw_rows = _ipowatch_parse_page(resp.text)
        if not raw_rows:
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        scraped_name = _ipowatch_slug_to_name(slug)
        db_name = strict_match(scraped_name, db_names)
        ipo_price = price_by_name.get(db_name) if db_name else None
        if not db_name:
            unmatched.append(scraped_name)

        upsert_rows = []
        for date_str, gmp_lo, gmp_hi in raw_rows:
            gmp_val = (gmp_lo + gmp_hi) / 2
            if date_str.lower() == "today":
                gmp_date = date.today().isoformat()
            else:
                try:
                    gmp_date = datetime.strptime(date_str, "%d-%m-%Y").date().isoformat()
                except ValueError:
                    continue
            est_listing_price = (ipo_price + gmp_val) if ipo_price else None
            est_profit_pct = round(100 * gmp_val / ipo_price, 2) if ipo_price else None
            upsert_rows.append((
                db_name or scraped_name, gmp_date, ipo_price, gmp_val, None,
                est_listing_price, est_profit_pct, None, None,
            ))
        if upsert_rows:
            _upsert_gmp_trend(c, upsert_rows)
            n_rows += len(upsert_rows)
        n_pages += 1
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    conn.commit()
    return {
        "source": "ipowatch", "pages_discovered": len(urls), "pages_scraped": n_pages,
        "rows_upserted": n_rows, "unmatched_companies": len(unmatched),
    }


# ---------------------------------------------------------------------------
# combined entry point
# ---------------------------------------------------------------------------
def run_gmp_sync(sources=("ipogyani", "ipowatch"), ipowatch_limit=None):
    conn = sqlite3.connect(config.DB_PATH)
    results = []
    errors = []
    if "ipogyani" in sources:
        try:
            results.append(sync_ipogyani(conn))
        except Exception as e:
            errors.append({"source": "ipogyani", "error": str(e)})
    if "ipowatch" in sources:
        try:
            results.append(sync_ipowatch(conn, limit=ipowatch_limit))
        except Exception as e:
            errors.append({"source": "ipowatch", "error": str(e)})

    # FIX: restore the gmp_percent push into ipo_master_records that
    # ipogyani_scraper.py used to do via backfill_gmp_percent_strict() --
    # run once at the end so it picks up rows from whichever source(s) ran.
    try:
        c = conn.cursor()
        n_updated = _backfill_master_from_gmp_trend(c)
        conn.commit()
        results.append({"source": "backfill_gmp_percent", "rows_updated": n_updated})
    except Exception as e:
        errors.append({"source": "backfill_gmp_percent", "error": str(e)})

    conn.close()
    return {"results": results, "errors": errors}



