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

  a) [partially resolved] NSE's URL/columns were wrong (confirmed via
     production logs: HTTP 404) -- NSE discontinued the old `cm<DATE>
     bhav.csv.zip` path on 2024-07-08 (Circular 62424) and switched to the
     "CM-UDiFF Common Bhavcopy Final" format. fetch_nse_bhavcopy() now uses
     that real URL/column set (see its own docstring). BSE's fetch got a
     Referer header added (its "not a zip" response looked like an
     HTML page being returned instead of the file, which a missing
     Referer/session context commonly causes) but its URL/column
     convention itself is still NOT independently verified against your
     historical parser -- if it's still failing after this, see
     fetch_bse_bhavcopy()'s docstring for the next thing to check.
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
def _fetch_zip_csv(url: str, extra_headers: dict | None = None) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0", **(extra_headers or {})}
    resp = requests.get(url, timeout=30, headers=headers)
    if resp.status_code != 200:
        logger.warning("Bhavcopy fetch %s -> HTTP %s", url, resp.status_code)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            return zf.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        logger.warning("Bhavcopy fetch %s -> not a zip (holiday / no file yet? or an HTML "
                        "error/login page instead of the file)", url)
        return None


# Equity cash-market series codes worth keeping a close price for. Excludes
# non-equity series NSE's combined UDIFF file also carries (debt, ETFs use
# their own series too but ETF closes aren't useful here). ASSUMPTION --
# not verified against your validated historical parser (see module
# docstring point (a)); EQ/BE/BZ are the standard NSE mainboard+SME/trade-
# to-trade equity series codes, but worth double-checking against a real
# downloaded file before trusting this blindly.
_NSE_EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}


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
    assume. Filters to _NSE_EQUITY_SERIES so index/derivative-adjacent rows
    in the combined file don't get mixed into the equity close-price map.
    Still worth a smoke test against a real recent date before fully
    trusting this -- see module docstring point (a)."""
    import csv

    url = (
        f"https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    )
    text = _fetch_zip_csv(url)
    if not text:
        return {}
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
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


def fetch_bse_bhavcopy(date: datetime.date) -> dict[str, float]:
    """{isin: close_price} for BSE equities on `date`, keyed by ISIN where
    present.

    FIX (2026-08-17): added a Referer header -- BSE's download endpoint has
    been known to return an HTML page (not the file) to requests that don't
    look like they came from a browser session on bseindia.com, which
    would explain the "not a zip" warning seen in production logs (as
    opposed to a real holiday/no-file-yet case). URL/column convention
    otherwise UNCHANGED from before and still NOT independently re-verified
    against your validated historical parser this session -- see module
    docstring point (a). If this still fails after the header fix, BSE's
    URL/date-suffix format itself may have changed (some sources suggest a
    settlement-type suffix like `_T0` was added at some point) and needs a
    real look at a fresh manual download from bseindia.com to confirm."""
    import csv

    url = f"https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{date.strftime('%d%m%y')}.zip"
    text = _fetch_zip_csv(url, extra_headers={
        "Referer": "https://www.bseindia.com/markets/equity/EQReports/BhavCopy.aspx",
        "Accept": "*/*",
    })
    if not text:
        return {}
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        isin = (row.get("ISIN_CODE") or row.get("ISIN") or "").strip()
        close = row.get("CLOSE") or row.get("CLOSE_PRICE")
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
        return {"date": date.isoformat(), "updated": 0, "no_bhavcopy_row": 0, "no_column_due": 0}

    updated, no_row, no_column = 0, 0, 0
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
            except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
                logger.warning("bhavcopy_sync failed for %r: %s", row["company_name"], e)
        conn.commit()
    finally:
        conn.close()

    result = {"date": date.isoformat(), "updated": updated, "no_bhavcopy_row": no_row, "no_column_due": no_column}
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
        return {"filled": 0, "skipped_not_due": 0, "api_failures": 0}

    filled, skipped, failures = 0, 0, 0
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
                result = {"filled": filled, "skipped_not_due": skipped, "api_failures": failures}
                logger.info("bhavcopy_sync.backfill_price_gaps: %s (stopped early)", result)
                return result
            break  # one horizon (and one API call) per company per run, oldest-due first

    result = {"filled": filled, "skipped_not_due": skipped, "api_failures": failures}
    logger.info("bhavcopy_sync.backfill_price_gaps: %s", result)
    return result
