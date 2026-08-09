"""
Live-fetch orchestrator.

Split of responsibility (per project decision):
  - IPO Guru:   pre-listing data -- dates, price band, subscription, GMP.
  - Indian API: post-listing data -- sector, PE/ROE/debt-equity, and the
                day1/2/3/5/10 + current price trail once the company lists.

fetch_and_upsert(company_name) is the single entry point both the on-demand
"search for a company we don't have" path and the background sync job call.
It's intentionally tolerant: either source failing (bad key, rate limit,
company simply not covered by that source) should not block the other
source's data from being saved.
"""

import datetime
import logging

from . import db
from .fetchers import ipoguru, indianapi

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


def fetch_and_upsert(company_name: str) -> dict:
    """Fetches whatever's available for `company_name` from both sources,
    merges it with whatever's already in the DB for that company (if any),
    upserts, and returns the resulting record as a dict.

    Raises nothing on a source-level failure -- those are logged and
    skipped so one flaky API doesn't take down the whole refresh. Only
    raises if BOTH sources come back empty for a name neither recognizes."""
    existing_record, _ = db.find_company(company_name)
    existing = existing_record.model_dump() if existing_record else {"company_name": company_name}

    ipoguru_partial = {}
    try:
        match = ipoguru.find_by_name(company_name)
        if match:
            ipoguru_partial = ipoguru.to_partial_record(match)
    except ipoguru.IPOGuruError as e:
        logger.warning("IPO Guru fetch failed for %r: %s", company_name, e)

    indianapi_partial = {}
    price_partial = {}
    try:
        stock = indianapi.fetch_stock(company_name)
        if stock:
            indianapi_partial = indianapi.to_partial_record(stock)
            listing_date = ipoguru_partial.get("listing_date") or existing.get("listing_date")
            if listing_date:
                history = indianapi.fetch_historical_prices(company_name)
                price_partial = indianapi.prices_by_offset(history, listing_date)
    except indianapi.IndianAPIError as e:
        logger.warning("Indian API fetch failed for %r: %s", company_name, e)

    if not ipoguru_partial and not indianapi_partial and not existing_record:
        raise LookupError(f"No data found for '{company_name}' from any source, and it isn't in the DB.")

    merged = _merge(existing, ipoguru_partial, indianapi_partial, price_partial)
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
