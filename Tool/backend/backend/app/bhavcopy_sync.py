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
    previously fired a bare requests.get() with no cookie handshake."""
    import csv

    session = _nse_session()
    url = (
        f"https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    )
    text = _fetch_zip_csv(url, extra_headers={"Referer": "https://www.nseindia.com/all-reports"}, session=session)
    if not text:
        return {}
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("FinInstrmTp") != "STK":
            continue
        isin = (row.get("ISIN") or "").strip()
        series = (row.get("SctySrs") or "").strip().upper()
        close = row.get("ClsPric")
        if isin and close and series in _NSE_EQUITY_SERIES:
            try:
                out[isin] = float(close)
            except ValueError:
                continue
    logger.info("NSE bhavcopy %s: %d ISINs parsed.", date.isoformat(), len(out))
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
            "SELECT company_name, isin, bse_script_code, listing_date, "
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


def _match_price(row: dict, nse_map: dict, bse_map: dict) -> float | None:
    """isin first, bse_script_code fallback -- exact matches only, no
    fuzzy/difflib (see db.strict_match()'s docstring on why that's banned
    for cross-company data joins in this project)."""
    isin = row.get("isin")
    if isin:
        merged = {**nse_map, **bse_map}
        if isin in merged:
            return merged[isin]
    code = row.get("bse_script_code")
    if code and code in bse_map:
        return bse_map[code]
    return None


def _update_if_null(conn, company_name: str, column: str, price: float) -> bool:
    cur = conn.execute(
        f"UPDATE ipo_master_records SET {column} = ? "
        f"WHERE company_name = ? AND {column} IS NULL",
        (price, company_name),
    )
    return cur.rowcount > 0


def run_bhavcopy_sync(target_date: datetime.date | None = None) -> dict:
    """Pass: previous trading day's close -> whichever price_dayN column
    is due for each trackable company, NULL cells only. Same
    try/except-per-source resilience convention as scheduler.py's other
    passes -- one bad row never stops the batch."""
    date = target_date or _previous_trading_day()
    nse_map = fetch_nse_bhavcopy(date)
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
        for row in companies:
            elapsed = elapsed_by_date.get(row["listing_date"])
            column = HORIZON_COLUMNS.get(elapsed)
            if column is None:
                no_column += 1
                continue
            if row.get(column) is not None:
                continue  # already filled, nothing to do
            try:
                price = _match_price(row, nse_map, bse_map)
                if price is None:
                    no_row += 1
                    continue
                if _update_if_null(conn, row["company_name"], column, price):
                    updated += 1
                    updated_companies.add(row["company_name"])
            except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
                logger.warning("bhavcopy_sync failed for %r: %s", row["company_name"], e)
        conn.commit()
    finally:
        conn.close()

    result = {"date": date.isoformat(), "updated": updated, "no_bhavcopy_row": no_row,
              "no_column_due": no_column, "updated_companies": sorted(updated_companies)}
    logger.info("bhavcopy_sync %s: %s", date.isoformat(), result)
    return result


# --- item 6: bounded gap-fill for rows bhavcopy never got a row for --------
def backfill_price_gaps() -> dict:
    """For each trackable company's next-due price_dayN cell (the earliest
    HORIZON_COLUMNS entry still NULL for that company), only once that
    horizon's due date is more than GAP_FILL_GRACE_DAYS in the past AND
    still NULL, spend ONE Indian API call to fill it -- not a routine
    per-refresh call, so the 500/month budget only goes to genuine gaps
    (illiquid SME counters, trading-halt days), same as item 6 asks."""
    today = datetime.date.today()
    companies, _ = get_trackable_companies(as_of=today)
    if not companies:
        return {"filled": 0, "skipped_not_due": 0, "api_failures": 0, "filled_companies": []}

    filled, skipped, failures = 0, 0, 0
    filled_companies = set()
    for row in companies:
        listing_date = datetime.date.fromisoformat(row["listing_date"][:10])
        for elapsed_due, column in sorted(HORIZON_COLUMNS.items()):
            if row.get(column) is not None:
                continue
            # Rough due-date estimate for this horizon: calendar days as a
            # stand-in for trading sessions. Good enough for a grace-period
            # check (GAP_FILL_GRACE_DAYS already builds in slack for
            # holidays/weekends); if tighter accuracy is ever needed, the
            # real NSE session calendar (_NSE, imported above) could instead
            # be walked forward from listing_date to find the Nth session.
            due_date = listing_date + datetime.timedelta(days=elapsed_due)
            if (today - due_date).days <= GAP_FILL_GRACE_DAYS:
                skipped += 1
                break  # earlier horizons aren't due yet either; later ones for this company skip too
            try:
                history = indianapi.fetch_historical_prices(row["company_name"])
                prices = indianapi.prices_by_offset(history, row["listing_date"])
                price = prices.get(column)
                if price is None:
                    failures += 1
                    continue
                conn = db.get_connection()
                try:
                    if _update_if_null(conn, row["company_name"], column, price):
                        filled += 1
                        filled_companies.add(row["company_name"])
                    conn.commit()
                finally:
                    conn.close()
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
                # happened.
                logger.warning(
                    "Gap-fill Indian API call failed for %r/%s: %s -- stopping this "
                    "run early (quota is account-wide, not per-company).",
                    row["company_name"], column, e,
                )
                failures += 1
                result = {"filled": filled, "skipped_not_due": skipped, "api_failures": failures,
                           "filled_companies": sorted(filled_companies)}
                logger.info("bhavcopy_sync.backfill_price_gaps: %s (stopped early)", result)
                return result
            break  # one horizon (and one API call) per company per run, oldest-due first

    result = {"filled": filled, "skipped_not_due": skipped, "api_failures": failures,
              "filled_companies": sorted(filled_companies)}
    logger.info("bhavcopy_sync.backfill_price_gaps: %s", result)
    return result
