"""
Live-fetch orchestrator.

Split of responsibility (per project decision):
  - ipoji.com:    pre-listing data -- price band + GMP-derived est_profit_pct,
                  subscription, and issue_category. Read from ipo_live_tracker
                  (see FIX LOG 5 below), which sync_ipoji_open_ipos()'s hourly
                  poll (scheduler.py) already scrapes and populates -- this
                  module does not hit ipoji.com over the network itself.
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
4. fetch_and_upsert() now skips the pre-listing partial entirely once a
   company's listing_date has passed (see _already_listed()) -- this
   source's data (price band, GMP, subscription total, subscription
   category breakdown, issue_category) is pre-listing by design (see
   module docstring above), so it should freeze at whatever it was when
   the company listed rather than keep getting re-fetched by
   sync_recent_listings()'s daily post-listing price_dayN passes.

--- FIX LOG (2026-08-15) ---
5. ipogyani.com is no longer used anywhere in this project -- pre-listing
   data now comes from ipoji.com instead (project decision). This also
   fixes a real bug: the old _ipogyani_partial() re-scraped ipogyani's
   full /live-ipo page from the network on EVERY call, and
   scheduler.sync_active_ipos() called fetch_and_upsert() once per active
   company -- ~13 identical page fetches per sync run for ~12 active
   IPOs. The new _ipoji_partial()/_ipoji_partial_from_row() never hit the
   network at all: they read the ipo_live_tracker table, which
   scheduler.sync_ipoji_open_ipos() already scrapes and populates once
   per sync run (see that function and scheduler.py's reordered
   run_sync_once()). fetch_and_upsert() also now accepts an optional
   `ipoji_row` so sync_active_ipos()'s loop can hand back the row it
   already has on hand, skipping even the DB lookup.

--- FIX LOG (2026-08-16) ---
6. The Indian API call (indianapi.fetch_stock()) was firing unconditionally
   for every company on every sync, including companies that hadn't listed
   yet -- despite this module's own docstring saying Indian API is
   post-listing-only data. _already_listed() (already defined below, and
   already used to gate the ipoji pre-listing partial) is now also used to
   gate this call. Since sync_active_ipos() runs fetch_and_upsert() once
   per ipo_live_tracker row (mostly pre-listing companies at any given
   time), this was the direct cause of both an oversized manual-sync
   duration and burning through the Indian API's 500-calls/month quota in
   a single run.
"""

import datetime
import logging

from . import db
from .fetchers import indianapi

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


def _ipoji_partial_from_row(row: dict) -> dict:
    """Maps one ipo_live_tracker row onto the ipo_master_records field
    names fetch_and_upsert() merges in. No network call here -- that row
    was already scraped and saved by sync_ipoji_open_ipos() /
    ipoji.poll_and_save_open_ipos() (see scheduler.py), so this is a pure
    reshape of data we already have.

    FIX (2026-08-17): listing_date/allotment_date used to be left unset
    here because ipo_live_tracker didn't store them -- true as of the
    2026-08-15 fix log above, but ipoji.py's upsert_live_tracker() gained
    both columns on 2026-08-16 and this mapping was never updated to
    match. Confirmed as the reason companies could never trip
    _already_listed() (which reads existing["listing_date"] off
    ipo_master_records, not ipo_live_tracker) via this path: with
    listing_date always dropped here, db.upsert_record() never persisted
    it, so _already_listed() stayed False forever for a given company no
    matter how long ago it actually listed -- gating out the Indian API
    fetch, price_dayN, and the "freeze pre-listing data" branch
    indefinitely, not just once."""
    return {
        "company_name": row.get("company_name"),
        "data_source": "ipoji",
        "price_band_upper": row.get("price_band_upper"),
        "gmp_percent": row.get("current_gmp_percent"),
        "open_date": row.get("open_date"),
        "close_date": row.get("close_date"),
        "listing_date": row.get("listing_date"),
        "allotment_date": row.get("allotment_date"),
        "subscription_total": row.get("current_subscription_total"),
        "subscription_qib": row.get("current_subscription_qib"),
        "subscription_hni": row.get("current_subscription_hni"),
        "subscription_rii": row.get("current_subscription_rii"),
        "issue_category": row.get("issue_category"),
    }


def _ipoji_partial(company_name: str) -> dict:
    """On-demand path -- used when fetch_and_upsert() is called for a
    company that wasn't already looked up as part of sync_active_ipos()'s
    loop (e.g. a user searching for a company we don't have yet), so no
    ipo_live_tracker row was handed to us directly.

    Looks the company up in ipo_live_tracker by name -- a local DB read,
    NOT a network fetch. Replaces the old _ipogyani_partial(), which used
    to hit ipogyani.com directly on every call; ipoji.com is only ever
    scraped by sync_ipoji_open_ipos()'s hourly poll now (see
    scheduler.py), and every other caller just reads what that poll
    already saved. Returns {} if the company isn't currently tracked as
    open (never appeared on ipoji's open pages, or it closed and was
    dropped -- see ipoji.remove_from_live_tracker())."""
    try:
        conn = db.get_connection()
    except Exception as e:
        logger.warning("Could not open DB connection for ipoji lookup of %r: %s", company_name, e)
        return {}
    try:
        cur = conn.execute("SELECT * FROM ipo_live_tracker WHERE company_name = ?", (company_name,))
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return _ipoji_partial_from_row(dict(row))


def _already_listed(listing_date: str | None) -> bool:
    """True if `listing_date` is set and is today or earlier -- i.e. the
    IPO has actually listed, bidding is long closed, and ipoji's
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


def fetch_and_upsert(company_name: str, ipoji_row: dict | None = None) -> dict:
    """Fetches whatever's available for `company_name` from both sources,
    merges it with whatever's already in the DB for that company (if any),
    upserts, and returns the resulting record as a dict.

    `ipoji_row` is an optional already-fetched ipo_live_tracker row (a
    dict) -- pass it when the caller already has one on hand (see
    scheduler.py's sync_active_ipos(), which loops over every
    ipo_live_tracker row and hands each straight back in here) so this
    function doesn't even need to run its own DB lookup, let alone a
    network call. When omitted (e.g. the on-demand "search for a company
    we don't have" path), this falls back to looking the company up in
    ipo_live_tracker itself via _ipoji_partial() -- still just a DB read.

    Raises nothing on a source-level failure -- those are logged and
    skipped so one flaky API doesn't take down the whole refresh. Only
    raises if BOTH sources come back empty for a name neither recognizes."""
    existing_record, _ = db.find_company(company_name)
    existing = existing_record.model_dump() if existing_record else {"company_name": company_name}

    ipoji_partial = {}
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
            "Skipping ipoji partial for %r -- already listed on %s, "
            "pre-listing data is frozen.", company_name, existing["listing_date"],
        )
    else:
        try:
            if ipoji_row is not None:
                ipoji_partial = _ipoji_partial_from_row(ipoji_row)
            else:
                ipoji_partial = _ipoji_partial(company_name)
        except Exception as e:  # noqa: BLE001 -- same "don't take down the rest" pattern as before
            logger.warning("ipoji lookup failed for %r: %s", company_name, e)

    # FIX (2026-08-16): this call used to fire unconditionally for every
    # company, every sync -- but per this module's own docstring, Indian
    # API only has POST-LISTING data (sector, PE/ROE/debt-equity, day1-10
    # price trail). _already_listed() was already defined and already used
    # above to gate the ipoji (pre-listing) partial -- it just wasn't also
    # used here to gate the Indian API (post-listing) partial, which is
    # the actual mirror-image mistake. Since sync_active_ipos() calls
    # fetch_and_upsert() once per row in ipo_live_tracker (which is almost
    # entirely pre-listing companies at any given time -- ~47 of them),
    # this was burning 70+ Indian API calls per sync run against a
    # 500/month quota, on data the module itself says isn't meaningful yet
    # for those companies.
    indianapi_partial = {}
    price_partial = {}
    if _already_listed(existing.get("listing_date")):
        try:
            stock = indianapi.fetch_stock(company_name)
            if stock:
                indianapi_partial = indianapi.to_partial_record(stock)
                history = indianapi.fetch_historical_prices(company_name)
                price_partial = indianapi.prices_by_offset(history, existing.get("listing_date"))
        except indianapi.IndianAPIError as e:
            logger.warning("Indian API fetch failed for %r: %s", company_name, e)
    else:
        logger.info(
            "Skipping Indian API fetch for %r -- not listed yet (or listing "
            "date unknown); Indian API only has post-listing data.", company_name,
        )

    if not ipoji_partial and not indianapi_partial and not existing_record:
        raise LookupError(f"No data found for '{company_name}' from any source, and it isn't in the DB.")

    merged = _merge(existing, ipoji_partial, indianapi_partial, price_partial)
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
