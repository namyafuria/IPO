"""
Live-fetch orchestrator.

Split of responsibility (per project decision):
  - ipogyani.com: pre-listing data -- price band + GMP-derived est_profit_pct,
    via the same live-IPO scrape gmp_sync.py already does for gmp_trend.
  - Indian API:   post-listing data -- sector, PE/ROE/debt-equity, and the
                  day1/2/3/5/10 + current price trail once the company lists.

fetch_and_upsert(company_name) is the single entry point both the on-demand
"search for a company we don't have" path and the background sync job call.
It's intentionally tolerant: either source failing (bad key, rate limit,
company simply not covered by that source) should not block the other
source's data from being saved.

--- FIX LOG (2026-08-12) ---
Swapped the IPO Guru pre-listing fetch for ipogyani.com, reusing
gmp_sync.py's _ipogyani_fetch_live() / _ipogyani_fetch_history() /
strict_match() rather than duplicating that scraping logic here.

This is a real coverage trade-off, not a like-for-like swap:
  - ipogyani.com's live-IPO page only lists issues CURRENTLY open for
    bidding today. IPO Guru's fetch_active_ipos() (still used by
    scheduler.py's sync_active_ipos()) also covered upcoming and
    recently-listed issues. A company that isn't live *today* now gets
    NOTHING from this path -- existing DB values are preserved either way
    (see _merge's never-overwrite-with-None rule), but a brand-new company
    that isn't currently open for bidding won't get any pre-listing data
    from this call until/unless it's live on a later one.
  - ipogyani has no open_date/close_date/allotment_date/listing_date and
    no subscription (QIB/HNI/RII) breakdown, unlike IPO Guru. Only
    price_band_upper and gmp_percent are populated now. Those other
    fields stay unfilled for freshly-fetched companies -- tracked as a
    still-open gap in the project file, same one gmp_sync.py's backfill
    doesn't cover either.
  - issue_category is NOT set by this path (ipogyani doesn't expose it) --
    a brand-new company fetched only through this path will fail
    /api/predict's "not tagged Mainboard or SME" check until something
    else sets it.
"""

import datetime
import logging

from . import db
from .fetchers import indianapi
from .gmp_sync import _ipogyani_fetch_live, _ipogyani_fetch_history, strict_match

logger = logging.getLogger("ipo_tool.live_fetch")


def _merge(existing: dict, *partials: dict) -> dict:
    """Layers partial dicts onto the existing DB row (existing may be {}
    for a brand-new company). Later partials win on conflict, but a partial
    field of None never overwrites a real existing value -- 'we didn't get
    this field this time' should never erase 'we had it before'."""
    merged = dict(existing)
    for partial in partials:
        for k, v in partial.items():
            if v is not None:
                merged[k] = v
    return merged


def _find_live_ipogyani_entry(company_name: str, live_listings: list[dict]) -> dict | None:
    """Match a queried company_name against today's ipogyani live-IPO list,
    using the same strict whole-word-substring matcher gmp_sync.py uses for
    every other company-identity match in this project (not difflib --
    see gmp_sync.py's module docstring for the false-positive reasoning)."""
    names = [e["company_name"] for e in live_listings]
    matched_name = strict_match(company_name, names)
    if matched_name is None:
        return None
    return next(e for e in live_listings if e["company_name"] == matched_name)


def _ipogyani_partial(company_name: str) -> dict:
    """Replaces the old IPO Guru pre-listing fetch. Returns {} for anything
    not live today (see module docstring's FIX LOG for the coverage
    trade-off) or on any request failure -- callers treat an empty dict the
    same as a source that simply had nothing to say."""
    try:
        live = _ipogyani_fetch_live()
    except Exception as e:
        logger.warning("ipogyani live-list fetch failed for %r: %s", company_name, e)
        return {}

    entry = _find_live_ipogyani_entry(company_name, live)
    if entry is None or not entry["slug"]:
        return {}

    partial = {"company_name": entry["company_name"], "data_source": "ipogyani"}
    if entry.get("price_band_high") is not None:
        partial["price_band_upper"] = entry["price_band_high"]

    try:
        history = _ipogyani_fetch_history(entry["slug"], entry.get("price_band_high"))
    except Exception as e:
        logger.warning("ipogyani history fetch failed for %r (%s): %s", company_name, entry["slug"], e)
        history = []

    # history rows: (gmp_date, ipo_price, gmp_value, subscription_at_snapshot,
    # est_listing_price, est_profit_pct, day_tag, last_updated). Take the
    # most recent row with a real est_profit_pct -- same "latest row per
    # company" logic gmp_sync.py's _backfill_master_from_gmp_trend() uses.
    latest_date, latest_pct = None, None
    for gmp_date, _price, _gmp, _sub, _listing, est_profit_pct, _tag, _upd in history:
        if est_profit_pct is None:
            continue
        if latest_date is None or gmp_date > latest_date:
            latest_date, latest_pct = gmp_date, est_profit_pct
    if latest_pct is not None:
        partial["gmp_percent"] = latest_pct

    return partial


def fetch_and_upsert(company_name: str) -> dict:
    """Fetches whatever's available for `company_name` from both sources,
    merges it with whatever's already in the DB for that company (if any),
    upserts, and returns the resulting record as a dict.

    Raises nothing on a source-level failure -- those are logged and
    skipped so one flaky API doesn't take down the whole refresh. Only
    raises if BOTH sources come back empty for a name neither recognizes."""
    existing_record, _ = db.find_company(company_name)
    existing = existing_record.model_dump() if existing_record else {"company_name": company_name}

    ipogyani_partial = {}
    try:
        ipogyani_partial = _ipogyani_partial(company_name)
    except Exception as e:  # noqa: BLE001 -- same "don't take down the rest" pattern as before
        logger.warning("ipogyani fetch failed for %r: %s", company_name, e)

    indianapi_partial = {}
    price_partial = {}
    try:
        stock = indianapi.fetch_stock(company_name)
        if stock:
            indianapi_partial = indianapi.to_partial_record(stock)
            listing_date = existing.get("listing_date")
            if listing_date:
                history = indianapi.fetch_historical_prices(company_name)
                price_partial = indianapi.prices_by_offset(history, listing_date)
    except indianapi.IndianAPIError as e:
        logger.warning("Indian API fetch failed for %r: %s", company_name, e)

    if not ipogyani_partial and not indianapi_partial and not existing_record:
        raise LookupError(f"No data found for '{company_name}' from any source, and it isn't in the DB.")

    merged = _merge(existing, ipogyani_partial, indianapi_partial, price_partial)
    merged["company_name"] = merged.get("company_name") or company_name
    merged["last_updated"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if merged.get("current_price") is not None:
        merged["current_price_asof"] = merged["last_updated"]

    # Compute listing_day_gain_pct if we now have both issue price and day1 price.
    issue_price = merged.get("price_band_upper")
    day1 = merged.get("price_day1")
    if issue_price and day1 and merged.get("listing_day_gain_pct") is None:
        try:
            merged["listing_day_gain_pct"] = round((float(day1) - float(issue_price)) / float(issue_price) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    record = db.IPORecord(**{k: v for k, v in merged.items() if k in db.IPO_COLUMNS})
    db.upsert_record(record)
    return record.model_dump()


def is_still_trackable(listing_date: str | None) -> bool:
    """True if a company listed recently enough that price_day2..10 /
    current_price are still worth re-fetching. Companies with no
    listing_date yet (still open/upcoming) are always trackable."""
    from . import config
    if not listing_date:
        return True
    try:
        d = datetime.date.fromisoformat(listing_date[:10])
    except ValueError:
        return True
    return (datetime.date.today() - d).days <= config.POST_LISTING_TRACK_DAYS
