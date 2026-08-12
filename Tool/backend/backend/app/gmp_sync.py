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
"""
import re
import sqlite3
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from . import config

HEADERS_IPOGYANI = {"User-Agent": "ipo-analyser-personal-project/1.0 (research use)"}
HEADERS_IPOWATCH = {"User-Agent": "Mozilla/5.0 (research script)"}

IPOGYANI_BASE = "https://ipogyani.com"
IPOGYANI_LIVE_URL = f"{IPOGYANI_BASE}/ipo-gmp-today"

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
    conn.close()
    return {"results": results, "errors": errors}


# ---------------------------------------------------------------------------
# scheduler.py integration -- add these two lines inside run_sync_once()
# (file not available this session, so not edited directly; paste in by hand):
#
#   from .gmp_sync import run_gmp_sync
#   run_gmp_sync(sources=("ipogyani",))   # keep ipowatch out of the automatic
#                                          # cron path until its first real
#                                          # run is confirmed -- trigger it
#                                          # manually via the endpoint below
#                                          # with a small `limit` first.
# ---------------------------------------------------------------------------
