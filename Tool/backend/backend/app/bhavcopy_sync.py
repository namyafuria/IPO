"""
Daily bhavcopy sync -- fills price_day1/2/3/5/10 for recently-listed
companies from NSE's (and BSE's) previous-day EOD bhavcopy file, instead
of a per-company live API call. See project plan §77-79 / predict_trajectory
FIX (2026-08-17) for why: the request-path Indian API call was pulled out
of predict_trajectory_smart_for_company() because it burned quota per
request and duplicated work a daily batch job can do once for everyone.

Design, per the 6-item build plan this implements:
  1. Download previous trading day's NSE + BSE bhavcopy, parse to
     {isin_or_symbol: close_price}, BSE preferred on conflict.
  2. Match against ipo_master_records rows still inside their Day1-10
     window, isin first then bse_script_code, EXACT ONLY -- no fuzzy/
     difflib matching (see db.py's strict_match() docstring for why that
     was banned from this project after it silently merged unrelated
     companies' data).
  3. Work out which price_dayN column today's close belongs in from
     trading sessions elapsed since listing_date, and fill it ONLY if
     that cell is currently NULL -- never overwrite an existing value.
  4. Wired into scheduler.py (run_bhavcopy_sync) and a new
     POST /api/sync/bhavcopy route (see the snippet at the bottom of this
     file / main_py_bhavcopy_route_snippet.py) guarded by the same
     _sync_lock pattern as /api/sync/gmp.
  5. (companion change, not in this file) predict_trajectory.py's
     request path no longer calls live_fetch.fetch_and_upsert()
     synchronously -- price data now arrives from this daily job instead.
  6. backfill_price_gaps(): a small, bounded fallback for rows bhavcopy
     never got a row for (illiquid SME counters, trading-halt days) --
     one Indian API call per genuinely-stuck (company, horizon), not a
     routine per-refresh call.

--- KNOWN GAPS / NOT YET VERIFIED THIS SESSION ---
routers_live.py was uploaded and reconciled against (see below) -- point
(b) from the original build is resolved. Two gaps remain, both because
the underlying files still haven't been uploaded:

  a) [NSE resolved, BSE round 3] NSE confirmed working in production
     (3,189 ISINs parsed on a real run). BSE's round-1/round-2 fixes
     (Referer header, session warm-up) never had a chance -- the real bug,
     confirmed by fetching BSE's own BhavCopy.aspx page this session, is
     that BSE discontinued its EQ_ISINCODE_<DDMMYY>.zip format on
     2024-07-08 (same date as NSE) and moved to the same UDiFF standard.
     fetch_bse_bhavcopy() now points at the real post-UDiFF URL (a plain
     .CSV, not a .zip) -- see that function's own docstring for the two
     independently-confirmed real URLs this was checked against, and the
     one thing NOT independently confirmed (the exact column names, since
     BSE's site blocked this session's attempts to fetch actual file
     content -- only the surrounding page was reachable).
  b) [resolved] _trading_days_elapsed_batch is now imported directly from
     the real routers_live.py (reuses its NSE session calendar via
     pandas_market_calendars, same one /ipos/listed uses -- correctly
     skips actual NSE holidays, not just weekends). get_trackable_companies()
     below was also rewritten to match /ipos/listed's own query shape
     (listing_date >= today-20-calendar-days pre-filter, then the batched
     elapsed-days call), per item 2's literal ask, rather than reusing
     live_fetch.is_still_trackable()'s separate POST_LISTING_TRACK_DAYS
     cutoff as the first draft did.
  c) config.py: assumed to expose DB_PATH (confirmed, used by db.py) --
     no new config keys required.
  d) _sync_lock / the /api/sync/gmp route pattern in main.py: not
     uploaded this session, so the route is written as a standalone
     snippet (main_py_bhavcopy_route_snippet.py) for you to paste in
     against the real lock object, rather than guessed at inline here.

--- FIX LOG (2026-08-18) -- item 2 (ISIN / NSE symbol on Listed cards) ---
e) Found a real, confirmable bug while checking this file for item 2:
   _match_price()'s bse_script_code fallback (`code in bse_map`) can NEVER
   match, because bse_map is built by fetch_bse_bhavcopy() as
   {isin: close_price} -- keyed by ISIN, not by BSE's numeric script code.
   A `code` here (e.g. "543938") is being looked up against a dict of ISIN
   strings (e.g. "INE0ABC01019"); that lookup fails 100% of the time.
   Confirmed from this file alone, no upload needed: fetch_bse_bhavcopy()'s
   own `out[isin] = float(close)` line is the only thing that populates
   bse_map. Practical effect: any row whose isin column is still NULL
   (exactly the brand-new, ipoji-only companies item 2 is about -- see
   live_fetch.py's FIX LOG 9) can never be matched by this job at all, so
   it never gets a price OR a backfilled identifier from bhavcopy, no
   matter how many days go by. Fixed below by removing the dead fallback
   (with a clear log line instead of a silent no-op) rather than leaving
   code that looks like it works but structurally cannot -- re-enabling a
   real script-code-keyed fallback needs BSE's actual scrip-code column
   name, which is still unconfirmed (see point (a) above).
f) Added the other half of item 2's ask ("confirm the sync actually
   writes ISIN/symbol into ipo_master_records instead of discarding it"):
   before this fix, it didn't -- this job only ever read isin/
   bse_script_code (to match) and only ever wrote price_dayN columns. NSE's
   UDiFF file carries the ticker symbol natively (TckrSymb, confirmed
   column per fetch_nse_bhavcopy()'s docstring) alongside ISIN and close
   price, so it costs nothing extra to also capture {isin: symbol} in the
   same pass and backfill ipo_master_records.nse_symbol (NULL-only, same
   safety convention as price_dayN) whenever a row is matched by ISIN.
   BSE-side symbol/script-code enrichment is NOT included here -- BSE's
   confirmed columns (point (a) above) don't include one, and inventing a
   column name for a file this session couldn't fetch content from would
   risk silently writing wrong data. That half stays a known gap until
   BSE's real column layout is confirmed.
"""

import datetime
import io
import logging
import zipfile

import requests

from . import db
from .fetchers import indianapi
from .routers_live import _NSE, _trading_days_elapsed_batch  # reuse, per items 2-3

logger = logging.getLogger("ipo_tool.bhavcopy_sync")

# --- price_dayN column selection -------------------------------------------
# Exact elapsed-trading-session counts that have a DB column. A close that
# lands on any other elapsed count (e.g. session 4, 6-9, or >10) has nowhere
# to go and is intentionally skipped -- see module docstring point 3.
HORIZON_COLUMNS = {1: "price_day1", 2: "price_day2", 3: "price_day3",
                   5: "price_day5", 10: "price_day10"}

# Same 20-calendar-day margin routers_live.get_listed_ipos() uses ahead of
# its own trading-day filter -- 10 NSE sessions is at most ~14 calendar
# days even across a long weekend/holiday cluster, so 20 stays a safe,
# generous pre-filter before the batched calendar call below.
_LISTED_WINDOW_CALENDAR_DAYS = 20

# How many extra calendar days past a horizon's due date to give bhavcopy
# before backfill_price_gaps() falls back to one Indian API call for that
# (company, horizon). ASSUMPTION -- not specified in the 6-item plan;
# picked to comfortably clear a long weekend. Tune freely.
GAP_FILL_GRACE_DAYS = 4

# FIX (2026-08-19): after this many failed Indian API lookups for the SAME
# (company, horizon) -- i.e. the API has genuinely returned "no price"
# this many separate runs -- stop spending an attempt on it every single
# run. Confirmed via production logs: the same ~11 companies (Ardee,
# LAPL, etc.) failed identically every run with no early-stop, meaning
# these are real "not found" responses, not transient/rate-limit errors --
# so retrying them daily forever was pure waste, crowding out companies
# that could actually be filled. GAP_FILL_RETRY_COOLDOWN_DAYS lets a
# skipped one be tried again eventually (data can catch up -- an illiquid
# SME counter may trade a few weeks later), rather than blacklisting for good.
GAP_FILL_MAX_ATTEMPTS = 3
GAP_FILL_RETRY_COOLDOWN_DAYS = 14


# --- previous trading day -----------------------------------------------
def _previous_trading_day(as_of: datetime.date | None = None) -> datetime.date:
    """Last real NSE session strictly before `as_of`, via the same shared
    _NSE calendar routers_live.py uses -- correctly skips actual NSE
    holidays, not just weekends, so this no longer needs its own naive
    Mon-Fri fallback."""
    as_of = as_of or datetime.date.today()
    sessions = _NSE.valid_days(start_date=as_of - datetime.timedelta(days=30), end_date=as_of)
    prior = [ts.date() for ts in sessions if ts.date() < as_of]
    if not prior:
        raise RuntimeError(f"No NSE session found in the 30 days before {as_of.isoformat()}.")
    return prior[-1]


# FIX (2026-08-19, backfill rewrite): exact due-date for a given horizon,
# via the same shared _NSE calendar, instead of backfill_price_gaps()'s old
# `listing_date + timedelta(days=elapsed_due)` calendar-day estimate. That
# estimate was only ever used for a grace-period check, so being off by a
# few days didn't matter -- but now this same due-date is also the exact
# date backfill fetches a HISTORICAL bhavcopy for, where being off by even
# one day means fetching the wrong day's file entirely. n=1 is listing day
# itself, matching this project's confirmed price_dayN convention (day1 =
# close ON the listing day, not the day after).
def _nth_trading_session(listing_date: datetime.date, n: int) -> datetime.date:
    sessions = _NSE.valid_days(
        start_date=listing_date,
        end_date=listing_date + datetime.timedelta(days=n * 3 + 15),  # generous margin for holidays
    )
    days = [ts.date() for ts in sessions if ts.date() >= listing_date]
    if len(days) < n:
        raise RuntimeError(
            f"Fewer than {n} NSE trading sessions found on/after {listing_date.isoformat()}."
        )
    return days[n - 1]


# FIX (2026-08-19, backfill rewrite): per-date cache for historical bhavcopy
# fetches within a single backfill_price_gaps() run. Different companies
# almost always have different listing_date -> different due dates, so this
# won't collapse many fetches into one most of the time, but it's a free
# safeguard against re-fetching the same date twice if two companies' due
# dates do happen to coincide (e.g. two IPOs listed the same week both
# having their Day5 due today).
def _get_historical_bhavcopy(
    date: datetime.date, cache: dict[datetime.date, tuple[dict, dict, dict]]
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Returns (nse_map, bse_map, nse_symbol_price_map) for `date`, built
    the same way run_bhavcopy_sync() builds them for "today" -- reused here
    unchanged so backfilled cells are matched by the exact same rules
    (isin first, nse_symbol fallback) as the routine daily sync."""
    if date in cache:
        return cache[date]
    nse_map: dict[str, float] = {}
    nse_symbol_price_map: dict[str, float] = {}
    for row in _fetch_nse_bhavcopy_rows(date):
        isin = (row.get("ISIN") or "").strip()
        close = row.get("ClsPric")
        symbol = (row.get("TckrSymb") or "").strip()
        if close:
            try:
                close_val = float(close)
            except ValueError:
                continue
            nse_map[isin] = close_val
            if symbol:
                nse_symbol_price_map[symbol] = close_val
    bse_map = fetch_bse_bhavcopy(date)
    result = (nse_map, bse_map, nse_symbol_price_map)
    cache[date] = result
    return result


# --- fetch + parse ---------------------------------------------------------
def _fetch_zip_csv(url: str, extra_headers: dict | None = None, session=None) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        **(extra_headers or {}),
    }
    requester = session or requests
    resp = requester.get(url, timeout=30, headers=headers)
    if resp.status_code != 200:
        logger.warning("Bhavcopy fetch %s -> HTTP %s", url, resp.status_code)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            return zf.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        # FIX (2026-08-17): "not a zip" alone wasn't enough to tell a real
        # holiday/no-file-yet case apart from BSE quietly serving an HTML
        # error/challenge page with a 200 status (the Referer-header fix
        # didn't resolve this in production, so it's worth actually seeing
        # what came back instead of guessing again). Logs the content-type
        # and first 200 bytes of the body -- if that's an HTML block/
        # captcha page, the real fix is the session warm-up below; if it's
        # genuinely empty/tiny, that's the holiday case.
        preview = resp.content[:200]
        logger.warning(
            "Bhavcopy fetch %s -> not a zip. content-type=%r, status=%s, first 200 bytes: %r",
            url, resp.headers.get("Content-Type"), resp.status_code, preview,
        )
        return None


# Equity cash-market series codes worth keeping a close price for. Excludes
# non-equity series NSE's combined UDIFF file also carries (debt, ETFs use
# their own series too but ETF closes aren't useful here). ASSUMPTION --
# not verified against your validated historical parser (see module
# docstring point (a)); EQ/BE/BZ are the standard NSE mainboard+SME/trade-
# to-trade equity series codes, but worth double-checking against a real
# downloaded file before trusting this blindly.
_NSE_EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}


def _nse_session() -> requests.Session:
    """Cookie handshake NSE requires before it'll serve the real bhavcopy
    URL -- confirmed necessary by your own working downloader script
    (session.get nseindia.com first, Referer on the follow-up request).
    fetch_nse_bhavcopy() below previously skipped this and relied on a
    bare requests.get(); it apparently worked once in production (3,189
    ISINs parsed per this module's earlier FIX LOG), but that's
    NSE-session-cookie-dependent behavior that can start 403ing without
    warning, so it's worth doing the handshake properly rather than
    relying on it working by luck."""
    session = requests.Session()
    warmup_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        session.get("https://www.nseindia.com", headers=warmup_headers, timeout=10)
    except requests.RequestException as e:
        logger.warning("NSE session warm-up failed (continuing anyway): %s", e)
    return session


def _fetch_nse_bhavcopy_rows(date: datetime.date) -> list[dict]:
    """Fetches + parses NSE's UDiFF bhavcopy once for `date`, filtered to
    the same STK + equity-series rows fetch_nse_bhavcopy() always has.
    Returns the raw dict rows (not just isin->close) so a single network
    fetch + parse can feed both the price map (fetch_nse_bhavcopy(), used
    for price_dayN) and the isin->symbol map (fetch_nse_isin_symbol_map(),
    used for item 2's nse_symbol backfill -- see FIX LOG point (f) above)
    without doubling the request."""
    import csv

    session = _nse_session()
    url = (
        f"https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    )
    text = _fetch_zip_csv(url, extra_headers={"Referer": "https://www.nseindia.com/all-reports"}, session=session)
    if not text:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("FinInstrmTp") != "STK":
            continue
        isin = (row.get("ISIN") or "").strip()
        series = (row.get("SctySrs") or "").strip().upper()
        if isin and series in _NSE_EQUITY_SERIES:
            rows.append(row)
    return rows


def fetch_nse_bhavcopy(date: datetime.date) -> dict[str, float]:
    """{isin: close_price} for NSE equities on `date`.

    FIX (2026-08-17): the old `cm<DDMMMYYYY>bhav.csv.zip` URL under
    /content/historical/EQUITIES/ was DISCONTINUED by NSE on 2024-07-08
    (NSE Circular 62424) -- confirmed via web search this session, and
    matches the HTTP 404 seen in production logs. NSE switched to the
    "CM-UDiFF Common Bhavcopy Final" format/URL instead:
        https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
    with a different column set (ISIN, TckrSymb, SctySrs, ClsPric, among
    others) than the old SYMBOL/SERIES/CLOSE layout this function used to
    assume. Requires FinInstrmTp == "STK" (excludes index/derivative rows
    the combined file also carries) -- SctySrs is checked too for NSE
    (EQ/BE/BZ/SM/ST are real NSE equity series codes), unlike BSE where
    that filter turned out to be wrong (see fetch_bse_bhavcopy()'s FIX
    LOG, round 4, for why BSE dropped it instead).

    FIX (round 2): added the NSE session warm-up (_nse_session()) your
    own confirmed-working downloader script uses -- this function
    previously fired a bare requests.get() with no cookie handshake.

    FIX (2026-08-18, item 2): now built on top of _fetch_nse_bhavcopy_rows()
    so the same parsed rows can also feed fetch_nse_isin_symbol_map()
    without a second network fetch -- behavior/return type here unchanged,
    still {isin: close_price}."""
    out = {}
    for row in _fetch_nse_bhavcopy_rows(date):
        isin = (row.get("ISIN") or "").strip()
        close = row.get("ClsPric")
        if close:
            try:
                out[isin] = float(close)
            except ValueError:
                continue
    logger.info("NSE bhavcopy %s: %d ISINs parsed.", date.isoformat(), len(out))
    return out


def fetch_nse_isin_symbol_map(date: datetime.date) -> dict[str, str]:
    """{isin: nse_symbol} for the same NSE UDiFF file fetch_nse_bhavcopy()
    reads, via TckrSymb -- a confirmed column in that file (see
    fetch_nse_bhavcopy()'s docstring). Added for item 2 (see FIX LOG point
    (f) above): lets run_bhavcopy_sync() backfill ipo_master_records.
    nse_symbol for any row matched by ISIN, at no extra fetch cost -- both
    this and fetch_nse_bhavcopy() build on _fetch_nse_bhavcopy_rows()."""
    out = {}
    for row in _fetch_nse_bhavcopy_rows(date):
        isin = (row.get("ISIN") or "").strip()
        symbol = (row.get("TckrSymb") or "").strip()
        if symbol:
            out[isin] = symbol
    return out


def _fetch_text(url: str, extra_headers: dict | None = None, session=None) -> str | None:
    """Plain (non-zipped) file fetch -- BSE's post-UDiFF bhavcopy is served
    as a raw .CSV, unlike NSE's .csv.zip (see fetch_bse_bhavcopy())."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        **(extra_headers or {}),
    }
    requester = session or requests
    resp = requester.get(url, timeout=30, headers=headers)
    if resp.status_code != 200:
        logger.warning("Bhavcopy fetch %s -> HTTP %s", url, resp.status_code)
        return None
    text = resp.text
    if "<html" in text[:200].lower():
        # Same failure mode as _fetch_zip_csv's BadZipFile branch -- an
        # HTML page (block/challenge/404-shell) came back instead of data.
        logger.warning(
            "Bhavcopy fetch %s -> looks like an HTML page, not CSV data. "
            "content-type=%r, first 200 chars: %r",
            url, resp.headers.get("Content-Type"), text[:200],
        )
        return None
    return text


def fetch_bse_bhavcopy(date: datetime.date) -> dict[str, float]:
    """{isin: close_price} for BSE equities on `date`, keyed by ISIN where
    present.

    FIX (2026-08-17), round 3: the URL itself was dead: BSE's
    `EQ_ISINCODE_<DDMMYY>.zip` (this function's original URL) was
    discontinued on 2024-07-08, the SAME date NSE discontinued its old
    format -- both exchanges moved to a common "UDiFF" standard together.
    BSE's new URL (now independently confirmed working against your own
    account and your own downloader script, and column layout confirmed
    directly from your uploaded BhavCopy_BSE_20250101.csv sample):
        https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{YYYYMMDD}_F_0000.CSV
    A PLAIN .CSV, not a .zip like NSE's (hence _fetch_text()).

    FIX (2026-08-17), round 4 -- THE REAL BUG, confirmed against your
    real sample file: round 3 correctly matched BSE's column NAMES to
    NSE's UDiFF layout (ISIN, SctySrs, ClsPric all present, confirmed),
    but wrongly assumed the column VALUES would also match -- filtering
    BSE's SctySrs against _NSE_EQUITY_SERIES ({"EQ","BE","BZ","SM","ST"}).
    BSE's real SctySrs values are its OWN group codes (confirmed from the
    sample: A, B, T, TS, X, Z, M, MT, R, G, F, IF, MS, P, ZP, XT) -- none
    of which overlap NSE's series codes at all. That silently dropped
    EVERY BSE row (confirmed: 0 of 4,411 rows survived the old filter on
    the sample file). Fix: filter on FinInstrmTp == "STK" only (all 4,411
    sample rows are already "STK" -- this column reliably separates
    equities from any non-equity instrument types the combined file might
    carry) and drop the series-based filter entirely for BSE -- there's no
    BSE-specific equivalent of "which series codes mean regular equity"
    worth hand-picking here, and FinInstrmTp already does that job."""
    session = requests.Session()
    try:
        session.get(
            "https://www.bseindia.com/",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
        )
    except requests.RequestException as e:
        logger.warning("BSE warm-up request failed (continuing anyway): %s", e)

    import csv

    url = (
        f"https://www.bseindia.com/download/BhavCopy/Equity/"
        f"BhavCopy_BSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.CSV"
    )
    text = _fetch_text(url, extra_headers={
        "Referer": "https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx",
        "Accept": "*/*",
    }, session=session)
    if not text:
        return {}
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("FinInstrmTp") != "STK":
            continue
        isin = (row.get("ISIN") or "").strip()
        close = row.get("ClsPric")
        if isin and close:
            try:
                out[isin] = float(close)
            except ValueError:
                continue
    logger.info("BSE bhavcopy %s: %d ISINs parsed.", date.isoformat(), len(out))
    return out


def build_close_price_map(date: datetime.date) -> dict[str, float]:
    """Merges NSE + BSE for `date`, BSE preferred on conflict (per your
    existing standardization -- see module docstring point (a))."""
    nse = fetch_nse_bhavcopy(date)
    bse = fetch_bse_bhavcopy(date)
    merged = dict(nse)
    merged.update(bse)  # BSE wins on overlap
    return merged


# --- matching + column selection -------------------------------------------
def get_trackable_companies(as_of: datetime.date | None = None) -> tuple[list[dict], dict]:
    """ipo_master_records rows still inside their Day1-10 window, using the
    SAME two-step shape as routers_live.get_listed_ipos(): a cheap SQL
    pre-filter on listing_date >= today-20-calendar-days, then one batched
    _trading_days_elapsed_batch() call over the whole candidate set (not
    live_fetch.is_still_trackable()'s separate POST_LISTING_TRACK_DAYS
    cutoff, which is a different window used for a different purpose
    elsewhere in this project).

    Returns (rows, elapsed_by_date) -- elapsed_by_date is the same
    {listing_date_str: elapsed_or_None} dict routers_live returns, keyed
    by each row's own (unparsed) listing_date string, so callers don't
    need to re-run the calendar call themselves."""
    as_of = as_of or datetime.date.today()
    cutoff = (as_of - datetime.timedelta(days=_LISTED_WINDOW_CALENDAR_DAYS)).isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "SELECT company_name, isin, bse_script_code, nse_symbol, listing_date, "
            "price_day1, price_day2, price_day3, price_day5, price_day10 "
            "FROM ipo_master_records "
            "WHERE listing_date IS NOT NULL AND listing_date != '' AND listing_date >= ? "
            "AND (price_day10 IS NULL OR price_day5 IS NULL "
            "     OR price_day3 IS NULL OR price_day2 IS NULL OR price_day1 IS NULL)",
            (cutoff,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    elapsed_by_date = _trading_days_elapsed_batch([r["listing_date"] for r in rows], as_of=as_of)
    # Day1-10 window, INCLUSIVE of day10 (unlike /ipos/listed's own elapsed<10,
    # which drops a company from that display list once day10 has elapsed --
    # we still need to catch the day the day10 close itself lands).
    rows = [r for r in rows if (elapsed_by_date.get(r["listing_date"]) or 0) in range(1, 11)]
    return rows, elapsed_by_date


def _match_price(row: dict, nse_map: dict, bse_map: dict,
                  nse_symbol_price_map: dict | None = None) -> float | None:
    """isin-only exact match first -- no fuzzy/difflib (see db.strict_match()'s
    docstring on why that's banned for cross-company data joins in this
    project).

    FIX (2026-08-18, item 2, see FIX LOG point (e) above): removed the
    bse_script_code fallback that used to sit here (`code in bse_map`) --
    bse_map is keyed by ISIN (fetch_bse_bhavcopy() builds it as
    {isin: close}), so a BSE script code could never appear as a key in
    it; that branch was dead code that could never match anything, not a
    working fallback.

    FIX (2026-08-19, Step 6): added an nse_symbol exact-match fallback for
    rows still missing isin. Step 5's symbol-matcher project backfilled
    nse_symbol/bse_script_code (not isin) for 442 rows -- without this
    fallback, none of that work was reachable from this price-matching
    job at all, since only isin was ever checked. NSE's UDiFF bhavcopy
    carries TckrSymb natively, so this costs nothing extra to build (see
    nse_symbol_price_map construction in run_bhavcopy_sync()). BSE-side
    (bse_script_code) fallback stays out -- BSE's actual scrip-code column
    name is still unconfirmed (see module docstring point (a)); guessing
    at it risks silently matching the wrong row, which is exactly what
    exact-only matching in this project exists to avoid."""
    isin = row.get("isin")
    merged = {**nse_map, **bse_map}
    if isin and isin in merged:
        return merged[isin]

    nse_symbol = row.get("nse_symbol")
    if nse_symbol and nse_symbol_price_map and nse_symbol in nse_symbol_price_map:
        return nse_symbol_price_map[nse_symbol]

    return None


def _update_if_null(conn, company_name: str, column: str, price: float) -> bool:
    cur = conn.execute(
        f"UPDATE ipo_master_records SET {column} = ? "
        f"WHERE company_name = ? AND {column} IS NULL",
        (price, company_name),
    )
    return cur.rowcount > 0


# --- FIX (2026-08-19): per-(company, horizon) attempt tracking -------------
# New, dedicated table rather than columns bolted onto ipo_master_records --
# this is bookkeeping for the gap-fill job itself, not IPO data, and doesn't
# need PRAGMA table_info gating like the nse_symbol column did.
def _ensure_gap_fill_attempts_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_gap_fill_attempts ("
        "company_name TEXT NOT NULL, column_name TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, last_attempted TEXT, "
        "PRIMARY KEY (company_name, column_name))"
    )


def _gap_fill_should_skip(conn, company_name: str, column: str, today: datetime.date) -> bool:
    """True once this (company, horizon) has failed GAP_FILL_MAX_ATTEMPTS+
    times AND the last attempt was within the cooldown window -- i.e. it's
    a known-stuck cell that isn't due for a retry yet."""
    row = conn.execute(
        "SELECT attempts, last_attempted FROM price_gap_fill_attempts "
        "WHERE company_name = ? AND column_name = ?",
        (company_name, column),
    ).fetchone()
    if not row or row["attempts"] < GAP_FILL_MAX_ATTEMPTS or not row["last_attempted"]:
        return False
    last = datetime.date.fromisoformat(row["last_attempted"][:10])
    return (today - last).days < GAP_FILL_RETRY_COOLDOWN_DAYS


def _gap_fill_record_attempt(conn, company_name: str, column: str, today: datetime.date) -> None:
    conn.execute(
        "INSERT INTO price_gap_fill_attempts (company_name, column_name, attempts, last_attempted) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(company_name, column_name) DO UPDATE SET "
        "attempts = attempts + 1, last_attempted = excluded.last_attempted",
        (company_name, column, today.isoformat()),
    )


def _gap_fill_clear_attempts(conn, company_name: str, column: str) -> None:
    """Called on a successful fill -- clears any stale failure history for
    this cell so a company that eventually gets a price isn't left with a
    dangling attempts row (harmless if it stays, just tidy)."""
    conn.execute(
        "DELETE FROM price_gap_fill_attempts WHERE company_name = ? AND column_name = ?",
        (company_name, column),
    )


def run_bhavcopy_sync(target_date: datetime.date | None = None) -> dict:
    """Pass: previous trading day's close -> whichever price_dayN column
    is due for each trackable company, NULL cells only. Same
    try/except-per-source resilience convention as scheduler.py's other
    passes -- one bad row never stops the batch."""
    date = target_date or _previous_trading_day()
    # FIX (2026-08-18, item 2): fetch NSE's rows once and derive both the
    # price map and the isin->symbol map from them, instead of calling
    # fetch_nse_bhavcopy() and fetch_nse_isin_symbol_map() separately (which
    # would each independently re-fetch the same file over the network).
    nse_rows = _fetch_nse_bhavcopy_rows(date)
    nse_map: dict[str, float] = {}
    nse_symbol_map: dict[str, str] = {}
    # FIX (2026-08-19, Step 6): also key close price by NSE symbol, so rows
    # backfilled by the symbol-matcher project (Step 5) but still missing
    # isin can be matched here too -- see _match_price()'s new fallback.
    nse_symbol_price_map: dict[str, float] = {}
    for row in nse_rows:
        isin = (row.get("ISIN") or "").strip()
        close = row.get("ClsPric")
        symbol = (row.get("TckrSymb") or "").strip()
        close_val = None
        if close:
            try:
                close_val = float(close)
                nse_map[isin] = close_val
            except ValueError:
                pass
        if symbol:
            nse_symbol_map[isin] = symbol
            if close_val is not None:
                nse_symbol_price_map[symbol] = close_val
    logger.info("NSE bhavcopy %s: %d ISINs parsed.", date.isoformat(), len(nse_map))
    bse_map = fetch_bse_bhavcopy(date)
    price_map = {**nse_map, **bse_map}

    companies, elapsed_by_date = get_trackable_companies(as_of=date)
    if not companies:
        logger.info("bhavcopy_sync: no trackable companies this cycle.")
        return {"date": date.isoformat(), "updated": 0, "no_bhavcopy_row": 0,
                "no_column_due": 0, "updated_companies": []}

    updated, no_row, no_column = 0, 0, 0
    updated_companies = set()
    conn = db.get_connection()
    try:
        # FIX (2026-08-18, item 2, see FIX LOG point (f) above): nse_symbol
        # backfill only runs if ipo_master_records actually has that
        # column -- checked once, up front, via PRAGMA table_info rather
        # than assumed, since schemas.py wasn't available this session to
        # confirm it's in IPO_COLUMNS. If it isn't there yet, this logs
        # once and simply skips the symbol backfill for this whole run
        # (price backfill below is completely unaffected either way) --
        # add it with `ALTER TABLE ipo_master_records ADD COLUMN
        # nse_symbol TEXT;` to turn the backfill on.
        has_nse_symbol_column = any(
            r["name"] == "nse_symbol" for r in conn.execute("PRAGMA table_info(ipo_master_records)")
        )
        if not has_nse_symbol_column:
            logger.warning(
                "bhavcopy_sync: ipo_master_records has no nse_symbol column -- "
                "skipping item-2 symbol backfill this run. Add it with "
                "ALTER TABLE ipo_master_records ADD COLUMN nse_symbol TEXT;"
            )

        for row in companies:
            elapsed = elapsed_by_date.get(row["listing_date"])
            column = HORIZON_COLUMNS.get(elapsed)
            if column is not None and row.get(column) is None:
                try:
                    price = _match_price(row, nse_map, bse_map, nse_symbol_price_map)
                    if price is None:
                        no_row += 1
                    elif _update_if_null(conn, row["company_name"], column, price):
                        updated += 1
                        updated_companies.add(row["company_name"])
                except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
                    logger.warning("bhavcopy_sync price update failed for %r: %s", row["company_name"], e)
            elif column is None:
                no_column += 1

            # FIX (2026-08-19, Step 7): complete the nse_symbol backfill --
            # nse_symbol_map and has_nse_symbol_column were both already
            # built above (2026-08-18) but never actually written anywhere;
            # this is the missing UPDATE. Runs for EVERY trackable company
            # each cycle (not just ones due for a price column today), isin
            # exact match, NULL-only -- so any newly-listed company gets its
            # nse_symbol auto-filled the first cycle after isin lands on its
            # row, with no manual CSV re-run needed. bse_script_code has no
            # equivalent daily source (BSE bhavcopy carries no scrip code,
            # per module docstring point (a)) -- stays manual for new listings.
            if has_nse_symbol_column and not row.get("nse_symbol"):
                isin = row.get("isin")
                symbol = nse_symbol_map.get(isin) if isin else None
                if symbol:
                    try:
                        if _update_if_null(conn, row["company_name"], "nse_symbol", symbol):
                            logger.info(
                                "bhavcopy_sync: backfilled nse_symbol=%s for %r via isin match",
                                symbol, row["company_name"],
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "bhavcopy_sync: nse_symbol backfill failed for %r: %s",
                            row["company_name"], e,
                        )
        conn.commit()
    finally:
        conn.close()

    result = {"date": date.isoformat(), "updated": updated, "no_bhavcopy_row": no_row,
              "no_column_due": no_column, "updated_companies": sorted(updated_companies)}
    logger.info("bhavcopy_sync %s: %s", date.isoformat(), result)
    return result


# --- item 6: bounded gap-fill for rows the routine daily sync never caught -
def backfill_price_gaps() -> dict:
    """For every trackable company's still-NULL price_dayN cells whose due
    date has already passed, first try a HISTORICAL bhavcopy for that exact
    due date (bhavcopy is free/unrated, so every due horizon is attempted,
    not just one per run) -- only past GAP_FILL_GRACE_DAYS, and only if
    bhavcopy genuinely has no row for that company on that date, does this
    fall back to ONE rate-limited Indian API call per company per run.

    FIX (2026-08-19, backfill rewrite): this replaces the Indian-API-only
    version of this function. Root cause it fixes (see project notes):
    run_bhavcopy_sync() only ever fetches the CURRENT day's bhavcopy and
    fills whichever single column is due TODAY -- it never revisits a day
    that already passed. A company whose Day1 came due before its isin/
    nse_symbol was backfilled (or before this job existed) was therefore
    permanently unreachable by the daily sync, no matter how many days went
    by -- its only path forward was this function, which previously only
    knew how to call the Indian API (slow, rate-limited to ~500/month,
    matched by company name rather than isin/symbol). Since NSE/BSE publish
    HISTORICAL bhavcopy files by date on request, the exact same trusted,
    free, exact-match path run_bhavcopy_sync() uses for "today" can be
    pointed at any past due-date instead -- so this now tries that first for
    every stuck cell, and the Indian API becomes the genuine last resort
    (illiquid counters / trading halts on the exact due date) it was always
    meant to be, rather than the only mechanism.

    Two earlier bugs (2026-08-19, still fixed here, now scoped to the API
    fallback path only):
    1. On a genuine "no price found" API result, the loop must move to the
       NEXT COMPANY, not keep trying this same company's later horizons in
       the same run (`break`, not `continue`) -- one stuck company must not
       burn multiple calls before the pass reaches a company that could
       actually be filled.
    2. A permanently-unresolvable (company, horizon) needs to age out --
       price_gap_fill_attempts tracking skips it for
       GAP_FILL_RETRY_COOLDOWN_DAYS after GAP_FILL_MAX_ATTEMPTS failures,
       rather than retrying it forever."""
    today = datetime.date.today()
    companies, _ = get_trackable_companies(as_of=today)
    empty_result = {"filled": 0, "filled_via_bhavcopy": 0, "filled_via_api": 0,
                     "skipped_not_due": 0, "skipped_unresolvable": 0,
                     "api_failures": 0, "filled_companies": []}
    if not companies:
        return empty_result

    conn = db.get_connection()
    _ensure_gap_fill_attempts_table(conn)
    conn.commit()

    filled_bhavcopy, filled_api, skipped, unresolvable, failures = 0, 0, 0, 0, 0
    filled_companies = set()
    bhavcopy_cache: dict[datetime.date, tuple] = {}
    try:
        for row in companies:
            listing_date = datetime.date.fromisoformat(row["listing_date"][:10])
            api_attempted_for_company = False  # one Indian API call per company per run, same budget as before
            for elapsed_due, column in sorted(HORIZON_COLUMNS.items()):
                if row.get(column) is not None:
                    continue
                try:
                    due_date = _nth_trading_session(listing_date, elapsed_due)
                except RuntimeError:
                    # Calendar doesn't have enough sessions yet after listing_date
                    # to know this horizon's due date -- treat like not-due-yet.
                    skipped += 1
                    break
                if due_date >= today:
                    skipped += 1
                    break  # earlier horizons weren't due yet either (monotonic in elapsed_due),
                    # so later ones for this company aren't due yet either -- next company.

                # --- try 1: historical bhavcopy for the exact due date, free/unrated ---
                nse_map, bse_map, nse_symbol_price_map = _get_historical_bhavcopy(due_date, bhavcopy_cache)
                price = _match_price(row, nse_map, bse_map, nse_symbol_price_map)
                if price is not None:
                    if _update_if_null(conn, row["company_name"], column, price):
                        filled_bhavcopy += 1
                        filled_companies.add(row["company_name"])
                    _gap_fill_clear_attempts(conn, row["company_name"], column)
                    conn.commit()
                    continue  # bhavcopy costs nothing -- keep checking this company's other due horizons

                # --- try 2: bhavcopy had no row for this company on this date ---
                if (today - due_date).days <= GAP_FILL_GRACE_DAYS:
                    # Too recent to conclude bhavcopy is genuinely missing it --
                    # could just be a same-week fetch timing thing. Give it more
                    # runs before spending an API call.
                    skipped += 1
                    continue
                if api_attempted_for_company:
                    continue  # already spent this run's one API call on this company
                if _gap_fill_should_skip(conn, row["company_name"], column, today):
                    unresolvable += 1
                    continue
                api_attempted_for_company = True
                try:
                    history = indianapi.fetch_historical_prices(row["company_name"])
                    prices = indianapi.prices_by_offset(history, row["listing_date"])
                    api_price = prices.get(column)
                    if api_price is None:
                        failures += 1
                        _gap_fill_record_attempt(conn, row["company_name"], column, today)
                        conn.commit()
                        continue  # this company's one API attempt is spent; other due
                        # horizons for it still get tried via bhavcopy above, next loop turn
                    if _update_if_null(conn, row["company_name"], column, api_price):
                        filled_api += 1
                        filled_companies.add(row["company_name"])
                    _gap_fill_clear_attempts(conn, row["company_name"], column)
                    conn.commit()
                except indianapi.IndianAPIError as e:
                    # FIX (2026-08-17): production logs showed this pass making
                    # (and losing) 7 individual Indian API calls in one run, all
                    # failing with the SAME "rate limit / credits exhausted"
                    # error -- that error is account-wide, not per-company, so
                    # once it's seen once, every remaining call this run is
                    # guaranteed to fail too. Stop the whole pass immediately
                    # instead of wasting the rest of the due companies' one
                    # attempt each -- they'll get picked up on the next run once
                    # quota is available again, same as if this run had never
                    # happened. Deliberately does NOT count toward
                    # price_gap_fill_attempts -- this is quota exhaustion, not
                    # evidence the (company, horizon) is unresolvable.
                    logger.warning(
                        "Gap-fill Indian API call failed for %r/%s: %s -- stopping API "
                        "fallback for the rest of this run (quota is account-wide, not "
                        "per-company). Bhavcopy-based fills already made this run are kept.",
                        row["company_name"], column, e,
                    )
                    failures += 1
                    result = {"filled": filled_bhavcopy + filled_api,
                              "filled_via_bhavcopy": filled_bhavcopy, "filled_via_api": filled_api,
                              "skipped_not_due": skipped, "skipped_unresolvable": unresolvable,
                              "api_failures": failures, "filled_companies": sorted(filled_companies)}
                    logger.info("bhavcopy_sync.backfill_price_gaps: %s (API fallback stopped early)", result)
                    return result
    finally:
        conn.close()

    result = {"filled": filled_bhavcopy + filled_api,
              "filled_via_bhavcopy": filled_bhavcopy, "filled_via_api": filled_api,
              "skipped_not_due": skipped, "skipped_unresolvable": unresolvable,
              "api_failures": failures, "filled_companies": sorted(filled_companies)}
    logger.info("bhavcopy_sync.backfill_price_gaps: %s", result)
    return result
