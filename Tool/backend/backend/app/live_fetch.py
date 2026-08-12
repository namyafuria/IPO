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
1. Swapped the IPO Guru pre-listing fetch for ipogyani.com, reusing
   gmp_sync.py's _ipogyani_fetch_live() / _ipogyani_fetch_history() /
   strict_match() rather than duplicating that scraping logic here.
2. Upgraded again to pull from gmp_sync.py's _ipogyani_fetch_live_status()
   (scrapes /live-ipo) instead of the GMP-only /ipo-gmp-today table. This
   closes most of the coverage gap noted in fix #1 below: open_date,
   close_date, allotment_date, listing_date, subscription_total, and
   issue_category are now populated from this path too, not just
   price_band_upper/gmp_percent. The still-open gap:
     - Coverage is still "currently on ipogyani's live-ipo page today"
       (open, awaiting allotment/listing, or upcoming) -- a company that's
       fully listed and dropped off that page gets nothing from this path
       on a later call. Existing DB values are preserved either way (see
       _merge's never-overwrite-with-None rule).
     - issue_category comes from THIS source now (Mainboard/SME), which
       fixes the /api/predict "not tagged" failure for brand-new companies
       that were previously only reachable via this path.
3. Added subscription_qib/subscription_hni/subscription_rii, via
   gmp_sync._ipogyani_fetch_subscription_categories() (a separate
   per-company page -- see that function's docstring for the confirmed
   caveat that its category breakdown can lag subscription_total, which
   still comes from _ipogyani_fetch_live_status() above, by some hours).
   These fields existed in schemas.py but were never populated by any
   fetch path before this.
4. fetch_and_upsert() now skips the ipogyani partial entirely once a
   company's listing_date has passed (see _already_listed()) -- all of
   ipogyani's data (price band, GMP, subscription total, subscription
   category breakdown, issue_category) is pre-listing by design (see
   module docstring above), so it should freeze at whatever it was when
   the company listed rather than keep getting re-fetched by
   sync_recent_listings()'s daily post-listing price_dayN passes.
"""

import datetime
import logging

from . import db
from .fetchers import indianapi
from .gmp_sync import _ipogyani_fetch_live_status

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


def _ipogyani_partial(company_name: str) -> dict:
    """Replaces the old IPO Guru pre-listing fetch. Pulls from ipogyani's
    /live-ipo page (via gmp_sync._ipogyani_fetch_live_status()), which
    covers open/awaiting-allotment/upcoming issues with real dates,
    subscription, and category -- not just GMP. Returns {} for anything
    not currently on that page, or on any request failure -- callers treat
    an empty dict the same as a source that simply had nothing to say."""
    try:
        live = _ipogyani_fetch_live_status()
    except Exception as e:
        logger.warning("ipogyani live-status fetch failed for %r: %s", company_name, e)
        return {}

    from .gmp_sync import strict_match
    names = [e["company_name"] for e in live]
    matched_name = strict_match(company_name, names)
    if matched_name is None:
        return {}
    entry = next(e for e in live if e["company_name"] == matched_name)

    partial = {"company_name": entry["company_name"], "data_source": "ipogyani"}
    field_map = {
        "price_band_high": "price_band_upper",
        "gmp_percent": "gmp_percent",
        "open_date": "open_date",
        "close_date": "close_date",
        "allotment_date": "allotment_date",
        "listing_date": "listing_date",
        "subscription_total": "subscription_total",
        "category": "issue_category",
    }
    for src_key, dest_key in field_map.items():
        if entry.get(src_key) is not None:
            partial[dest_key] = entry[src_key]

    # FIX (2026-08-12): subscription_qib/subscription_hni/subscription_rii
    # were declared in schemas.py but never actually fetched by anything --
    # see gmp_sync._ipogyani_fetch_subscription_categories()'s docstring
    # for the confirmed caveat that this source's category breakdown can
    # lag subscription_total (set above, from a different ipogyani page)
    # by some hours. Self-contained/catches its own request errors, so a
    # failure here never blocks the rest of this partial.
    from .gmp_sync import _ipogyani_fetch_subscription_categories
    partial.update(_ipogyani_fetch_subscription_categories(entry["slug"]))

    return partial


def _already_listed(listing_date: str | None) -> bool:
    """True if `listing_date` is set and is today or earlier -- i.e. the
    IPO has actually listed, bidding is long closed, and ipogyani's
    pre-listing data (price band, GMP, subscription -- see module
    docstring's source split) is no longer meaningful to keep refreshing.
    A future listing_date (still upcoming) or no listing_date at all
    (still open/awaiting allotment) both return False -- same date-parsing
    pattern as is_still_trackable() below."""
    if not listing_date:
        return False
    try:
        d = datetime.date.fromisoformat(listing_date[:10])
    except ValueError:
        return False
    return d <= datetime.date.today()


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
    if _already_listed(existing.get("listing_date")):
        # FIX (2026-08-12): once an IPO has actually listed, ipogyani's
        # price-band/GMP/subscription data (price_band_upper, gmp_percent,
        # subscription_total, subscription_qib/hni/rii, issue_category) is
        # frozen -- don't keep re-fetching it. Before this, sync_recent_
        # listings()'s daily post-listing passes (run purely to fill
        # price_dayN) also re-ran the full ipogyani partial every time,
        # which could silently drift subscription_qib/hni/rii away from
        # their real final values if ipogyani's per-company page ever
        # recomputes/changes after close (see _ipogyani_fetch_
        # subscription_categories()'s cadence-lag caveat) -- there's no
        # reason to re-poll a pre-listing-only source once listing has
        # already happened. This does NOT affect gmp_trend/gmp_percent's
        # separate live-tracking path (run_gmp_sync(), scheduler.py's
        # sync_gmp_trend() pass) -- that's a different, intentionally
        # ongoing mechanism, untouched by this.
        logger.info(
            "Skipping ipogyani fetch for %r -- already listed on %s, "
            "pre-listing data is frozen.", company_name, existing["listing_date"],
        )
    else:
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
