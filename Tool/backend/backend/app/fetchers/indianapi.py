"""
Indian API (indianapi.in) client -- source of post-listing data: sector,
PE/ROE/debt-equity, and the price trail (day1/2/3/5/10, current price)
once a company has actually listed.

Docs: https://indianapi.in/documentation/indian-stock-market
Free key: sign up at indianapi.in, subscribe to the free tier, take the
key from the dashboard. Free/Hobby plans call https://stock.indianapi.in.

SCHEMA CONFIRMATION (2026-08-09): /stock's top-level shape and
/historical_data's request params + response shape were confirmed against
real third-party integration code (not just marketing copy). Then, same
day, a real live /stock response (Swiggy) was captured and reconciled --
this fixed two more real mismatches: keyMetrics is nested groups of lists,
not a flat dict (see _flatten_key_metrics/to_partial_record), and
isin/bse/nse live under companyProfile as isInId/exchangeCodeBse/
exchangeCodeNse, not the guessed isin/bseCode/nseSymbol. See
to_partial_record's docstring for the full confirmed/unconfirmed picture.
"""

import re
import requests

from .. import config

_session = requests.Session()


class IndianAPIError(Exception):
    pass


def _headers():
    if not config.INDIANAPI_API_KEY:
        raise IndianAPIError("INDIANAPI_API_KEY is not set")
    return {"X-Api-Key": config.INDIANAPI_API_KEY}


def fetch_stock(company_name: str) -> dict | None:
    """GET /stock?name=<company>. Supports full/short/common names per
    Indian API's own docs. Returns None on a clean 404 (not listed /
    not found) rather than raising, since 'not found here' is an expected,
    routine outcome for a company that hasn't listed yet."""
    resp = _session.get(
        f"{config.INDIANAPI_BASE_URL}/stock",
        headers=_headers(),
        params={"name": company_name},
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        raise IndianAPIError("Indian API rate limit / credits exhausted")
    resp.raise_for_status()
    data = resp.json()
    return data or None


def fetch_historical_prices(company_name: str, period: str = "1m") -> list[list]:
    """GET /historical_data?stock_name=<company>&period=<period>&filter=price.
    CONFIRMED shape (verified against real third-party integration code, not
    just docs): params are `stock_name` (not `name`) + `period` (one of 1m/
    6m/1yr/3yr/5yr/10yr/max) + `filter` (one of price/pe/sm/evebitda/ptb/mcs
    -- 'price' is what we want here). Response is
    `{"datasets": [{"metric": "Price", "label": "Price on NSE",
    "values": [["2024-06-27", "3934.15"], ...]}, {"metric": "DMA50", ...}]}`
    -- multiple datasets (Price, DMA50, DMA200, etc.), each a list of
    [date_str, price_str] pairs, not a list of dicts. This function returns
    just the "Price" dataset's `values` list; empty list if unavailable
    rather than raising, since a freshly-listed company may not have
    history yet."""
    resp = _session.get(
        f"{config.INDIANAPI_BASE_URL}/historical_data",
        headers=_headers(),
        params={"stock_name": company_name, "period": period, "filter": "price"},
        timeout=15,
    )
    if resp.status_code in (404, 400):
        return []
    if resp.status_code == 429:
        raise IndianAPIError("Indian API rate limit / credits exhausted")
    resp.raise_for_status()
    data = resp.json()
    datasets = data.get("datasets") or []
    price_dataset = next((d for d in datasets if d.get("metric") == "Price"), None)
    if not price_dataset:
        return []
    return price_dataset.get("values") or []


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_key(k: str) -> str:
    """keyMetrics's own key names are inconsistent -- some have stray
    trailing punctuation (e.g. 'returnOnAverageEquityMostRecentFiscalYear)'
    literally ends in ')', confirmed 2026-08-09 against a real response).
    Strip everything but lowercase alnum so lookups aren't tripped up by that."""
    return re.sub(r"[^a-z0-9]", "", k.lower())


def _flatten_key_metrics(key_metrics: dict) -> dict:
    """CONFIRMED shape (2026-08-09, real /stock response for Swiggy):
    keyMetrics is NOT a flat dict of ratios. It's ~8 named groups
    (mgmtEffectiveness, margins, financialstrength, valuation,
    incomeStatement, growth, persharedata, priceandVolume), each a LIST of
    {'displayName', 'key', 'value'} dicts. Flattens all groups into one
    {normalized_key: value} dict so a single lookup can search across all
    of them without caring which group a ratio happens to live in."""
    flat = {}
    if not isinstance(key_metrics, dict):
        return flat
    for group_items in key_metrics.values():
        if not isinstance(group_items, list):
            continue
        for item in group_items:
            if not isinstance(item, dict) or "key" not in item:
                continue
            flat[_normalize_key(item["key"])] = item.get("value")
    return flat


def _first_present(flat_metrics: dict, candidates: list[str]):
    """Try each candidate key (already un-normalized, human-typed) in
    priority order against the flattened+normalized keyMetrics; return the
    first one whose value isn't None/empty. Candidates are normalized here
    too, so exact casing/punctuation in the candidate list doesn't matter."""
    for c in candidates:
        v = flat_metrics.get(_normalize_key(c))
        if v is not None:
            return v
    return None


def to_partial_record(stock: dict) -> dict:
    """Maps one Indian API /stock response onto ipo_master_records columns.

    CONFIRMED top-level shape (verified against a real response, Swiggy,
    2026-08-09): {"tickerId": ..., "companyName": ..., "industry": "...",
    "companyProfile": {...}, "currentPrice": {"BSE": .., "NSE": ..},
    "keyMetrics": {...}, "recentNews": [...], ...}. `industry` is
    confirmed top-level. `tickerId` can be None even for a listed company
    (confirmed -- Swiggy's response had it as None), so it's a fallback
    source for nse_symbol, not a primary one.

    CONFIRMED companyProfile keys (2026-08-09): companyDescription,
    mgIndustry, isInId, officers, exchangeCodeBse, exchangeCodeNse,
    peerCompanyList, dataStatus, lastSuccessfulRefresh, lastRefreshAttempt,
    refreshPending, refreshError, fallbackSections. Real key names for
    isin/bse/nse are isInId/exchangeCodeBse/exchangeCodeNse -- NOT
    isin/bseCode/nseSymbol as originally guessed. No city field present
    anywhere in this response; left unconfirmed (None) rather than guessed.

    CONFIRMED keyMetrics shape: nested groups of lists, not a flat dict --
    see _flatten_key_metrics(). PE/ROE/debt-equity extracted via
    _first_present() over the flattened+normalized metrics, trying the
    most-recent-fiscal-year variant first and falling back to
    trailing-12-month / 5-year-average variants. A None result for PE is
    expected and correct for loss-making companies (undefined P/E)."""
    profile = stock.get("companyProfile") or {}
    price = stock.get("currentPrice") or {}
    flat_metrics = _flatten_key_metrics(stock.get("keyMetrics") or {})

    current_price = _num(price.get("NSE") or price.get("BSE"))

    pe = _first_present(flat_metrics, [
        "pPerEExcludingExtraordinaryItemsMostRecentFiscalYear",
        "pPerENormalizedMostRecentFiscalYear",
        "pPerEIncludingExtraordinaryItemsTTM",
        "pPerEBasicExcludingExtraordinaryItemsTTM",
    ])
    roe = _first_present(flat_metrics, [
        "returnOnAverageEquityMostRecentFiscalYear",
        "returnOnAverageEquityTrailing12Month",
        "returnOnAverageEquity5YearAverage",
    ])
    debt_equity = _first_present(flat_metrics, [
        "totalDebtPerTotalEquityMostRecentFiscalYear",
        "totalDebtPerTotalEquityMostRecentQuarter",
        "ltDebtPerEquityMostRecentFiscalYear",
        "ltDebtPerEquityMostRecentQuarter",
    ])

    return {
        "sector": stock.get("industry") or profile.get("mgIndustry"),
        "pe_ratio": _num(pe),
        "roe": _num(roe),
        "debt_equity": _num(debt_equity),
        "current_price": current_price,
        "isin": profile.get("isInId"),
        "bse_script_code": profile.get("exchangeCodeBse"),
        "nse_symbol": profile.get("exchangeCodeNse") or stock.get("tickerId"),
        "city": None,  # not present anywhere in a confirmed real response; unconfirmed
    }


def prices_by_offset(history: list[list], listing_date: str) -> dict:
    """Given the "Price" dataset's `values` list from fetch_historical_prices
    (confirmed shape: `[["2024-06-27", "3934.15"], ["2024-06-28", "3904.15"],
    ...]` -- [date_str, price_str] pairs) and the listing_date (YYYY-MM-DD),
    picks out the close on TRADING days 1/2/3/5/10 -- counting the listing
    day itself as day 1, per the trading-day convention verified project-wide
    in §66 against known bhavcopy rows (Swiggy, Hyundai Motor India, Mankind
    Pharma: price_dayN = close on the Nth trading day, listing day = day 1).
    This must stay a trading-day INDEX, not a calendar-day offset -- a
    calendar-day version (base + timedelta(days=N)) would silently label
    'day1' as the day *after* listing, which wouldn't match the price_dayN
    values already in the DB from the bhavcopy backfill. Mixing the two
    conventions in the same column would corrupt any model trained on both.
    """
    import datetime

    if not history or not listing_date:
        return {}

    try:
        base = datetime.date.fromisoformat(listing_date[:10])
    except ValueError:
        return {}

    by_date = {}
    for pair in history:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        d_str, price_str = pair[0], pair[1]
        try:
            d = datetime.date.fromisoformat(str(d_str)[:10])
        except ValueError:
            continue
        price = _num(price_str)
        if price is not None:
            by_date[d] = price

    # Trading days on/after listing, in order. If the exact listing date
    # isn't itself a returned trading day (holiday, data gap), day 1 falls
    # back to the nearest trading day at or after it -- same tolerant
    # behavior as before, just re-anchored to a trading-day index (day N =
    # the Nth entry in this list) instead of a calendar-day target.
    trading_days = sorted(d for d in by_date if d >= base)

    out = {}
    for n, col in [(1, "price_day1"), (2, "price_day2"), (3, "price_day3"),
                   (5, "price_day5"), (10, "price_day10")]:
        idx = n - 1  # day 1 == trading_days[0]
        if idx < len(trading_days):
            out[col] = by_date[trading_days[idx]]
    return out
