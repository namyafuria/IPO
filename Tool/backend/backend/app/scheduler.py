"""
Background sync -- periodically refreshes:
  1. Every IPO currently in ipo_live_tracker (i.e. everything ipoji.com's
     open pages had on the latest poll -- see sync_ipoji_open_ipos()), so
     the DB stays current on subscription/GMP/dates without anyone having
     to search for it. (Was IPO Guru until 2026-08-12, then ipogyani until
     2026-08-15 -- see sync_active_ipos().)
  2. Every DB row still within POST_LISTING_TRACK_DAYS of its listing_date,
     so price_day2/3/5/10 and current_price fill in as the days pass.
  3. Live GMP-trend history (ipogyani.com) for every currently-live IPO,
     into gmp_trend -- see gmp_sync.py. Only ipogyani runs automatically
     here; ipowatch.in is a second GMP source that's never been run
     end-to-end against the live site, so it's deliberately left out of
     this automatic path. Trigger it manually first via
     POST /api/sync/gmp?sources=ipowatch&ipowatch_limit=15 (small limit --
     see that endpoint's docstring for why) and confirm it behaves before
     ever adding it here.

Runs in-process via APScheduler. On a serverless host (Vercel functions),
in-process background loops don't persist between invocations -- see the
note at the bottom for that case.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, db, live_fetch
from .gmp_sync import run_gmp_sync
from .fetchers import ipoji
from .bhavcopy_sync import run_bhavcopy_sync, backfill_price_gaps
from .trajectory_predictions_store import save_trajectory_prediction
from .predict_trajectory import TrajectoryPredictionError

logger = logging.getLogger("ipo_tool.scheduler")


def sync_active_ipos():
    """Pass 1: everything currently in ipo_live_tracker -- i.e. every IPO
    ipoji.com's open pages had on the most recent poll (see
    sync_ipoji_open_ipos() / ipoji.poll_and_save_open_ipos()).

    FIX (2026-08-15): ipogyani is no longer used anywhere in this project
    -- this pass now reads the already-scraped ipo_live_tracker table
    (a local DB read) instead of calling gmp_sync._ipogyani_fetch_live_
    status() over the network. Two things this fixes together:
      1. The active-IPO source is ipoji now, per project decision.
      2. This is also what kills the old 12x-redundant-fetch bug: pass 4
         (sync_ipoji_open_ipos, now moved BEFORE this pass in
         run_sync_once() -- see below) already scrapes every open
         company's slug once each (3 requests/slug + rate-limit delays)
         to populate ipo_live_tracker. This pass just reuses those rows
         instead of re-fetching per company. Each row is passed straight
         into fetch_and_upsert() as `ipoji_row` so live_fetch.py doesn't
         even need a per-company DB query, let alone a network call.

    NOTE: on the very first sync after a fresh deploy, ipo_live_tracker
    will be empty until pass 4 has run once, so this pass will
    legitimately find nothing to do that cycle. It self-heals the next
    cycle (run_sync_once() always runs pass 4 first now) -- not handled
    specially beyond the warning below."""
    conn = db.get_connection()
    try:
        cur = conn.execute("SELECT * FROM ipo_live_tracker")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        logger.warning(
            "ipo_live_tracker is empty -- nothing for sync_active_ipos to do this cycle "
            "(expected on the very first run, before sync_ipoji_open_ipos has polled once)."
        )
        return

    for row in rows:
        name = row.get("company_name")
        if not name:
            continue
        # FIX (2026-08-17): skip rows that have already listed. ipoji's
        # pre-listing data is frozen for these anyway (live_fetch's own
        # _already_listed() gate already skips that half) -- but
        # fetch_and_upsert() ALSO still fires an Indian API call for any
        # already-listed row (its post-listing branch), and this loop runs
        # EVERY cron cycle (POST /api/sync, on a 4-hour external cron) for
        # EVERY row still sitting in ipo_live_tracker. That's what was
        # burning the Indian API's 500/month quota in production
        # (confirmed via Render logs 2026-08-17: repeated "Indian API rate
        # limit / credits exhausted" warnings) -- not the per-request call
        # in predict_trajectory.py, which was already removed. Post-listing
        # price data (price_day1..10) is now bhavcopy_sync.py's job,
        # running once/day for free off the bulk EOD file, so there's
        # nothing left for this pass to usefully fetch from Indian API for
        # an already-listed company -- skip it entirely rather than call
        # fetch_and_upsert() only to have it internally no-op on ipoji and
        # burn one Indian API call for nothing.
        if live_fetch._already_listed(row.get("listing_date")):
            continue
        try:
            live_fetch.fetch_and_upsert(name, ipoji_row=row)
        except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
            logger.warning("Sync failed for %r: %s", name, e)


def sync_recent_listings():
    """Pass 2 -- DISABLED (2026-08-17): this pass existed ONLY to fill
    price_dayN/current_price via Indian API for recently-listed companies.
    bhavcopy_sync.py's daily run (Pass 5) now does exactly this job --
    for free, in bulk, off the previous day's EOD bhavcopy file -- with
    its own bounded Indian API gap-fill fallback (backfill_price_gaps())
    for the rare row bhavcopy genuinely never got a row for. Calling this
    pass too meant hitting Indian API for the SAME companies bhavcopy_sync
    was already covering, on every 4-hour /api/sync cron run, which was
    the other half of the quota-exhaustion problem alongside
    sync_active_ipos() above.
    Function kept (unused by run_sync_once() below) rather than deleted,
    in case it's ever needed for a manual one-off backfill."""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT company_name, listing_date FROM ipo_master_records "
            "WHERE listing_date IS NOT NULL "
            "AND (price_day10 IS NULL OR current_price IS NULL)"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        if not live_fetch.is_still_trackable(row["listing_date"]):
            continue
        try:
            live_fetch.fetch_and_upsert(row["company_name"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Recent-listing sync failed for %r: %s", row["company_name"], e)


def sync_gmp_trend():
    """Pass 3: live day-wise GMP history for currently-live IPOs, into
    gmp_trend. ipogyani only -- see module docstring for why ipowatch is
    excluded from this automatic path. Bounded/small (only currently-live
    IPOs), so safe to run every cycle on any host."""
    try:
        result = run_gmp_sync(sources=("ipogyani",))
        logger.info("GMP trend sync: %s", result)
    except Exception as e:  # noqa: BLE001 -- same "don't take down the batch" pattern as passes 1/2
        logger.warning("GMP trend sync failed: %s", e)


def sync_ipoji_open_ipos():
    """Pass 4 (Step 3): IPO Ji live poll -- discovers every currently-open
    IPO on ipoji.com and upserts gmp_trend / subscription_daywise /
    ipo_live_tracker (see app/services/ipoji.py). Same 'one bad source
    shouldn't stop the batch' resilience pattern as passes 1-3.

    Unlike ipowatch (see module docstring -- deliberately opt-in-only via
    POST /api/sync/gmp until proven against the live site), this source
    has already been smoke-tested (Step 1/2), so it's wired straight into
    the automatic cadence rather than held back as manual-only.

    Runs on its OWN hourly job in start_scheduler() below, separate from
    SYNC_INTERVAL_MINUTES -- Step 3 asked for this specifically on an
    hourly cadence, which SYNC_INTERVAL_MINUTES isn't guaranteed to match.
    It's also called from run_sync_once() so the existing manual /api/sync
    button fires it too (the 'on demand' requirement) -- meaning on a host
    where both the hourly job and a manual click land close together, IPO
    Ji could get polled twice in quick succession. That's wasted work, not
    a correctness problem (Step 1's upsert keys make a repeat poll a no-op
    beyond overwriting with the same fresh values), so left as-is rather
    than adding de-dupe/locking complexity for a rare, harmless overlap."""
    try:
        result = ipoji.poll_and_save_open_ipos()
        logger.info(
            "IPO Ji live sync: %d companies saved, %d unresolved names, %d fetch errors.",
            len(result["companies_saved"]),
            len(result["unresolved_company_names"]),
            len(result["fetch_errors"]),
        )
        if result["unresolved_company_names"]:
            # Not a failure -- these companies still got saved under their
            # slug-derived name (see resolve_company_name() in ipoji.py) --
            # but they didn't line up with an existing ipo_master_records
            # row, so they're worth a human glance rather than silent trust.
            logger.warning(
                "IPO Ji sync: unresolved company names (using slug-derived name instead): %s",
                result["unresolved_company_names"],
            )
        return result
    except Exception as e:  # noqa: BLE001 -- same "don't take down the batch" pattern as passes 1-3
        logger.warning("IPO Ji live sync failed: %s", e)
        return None


def sync_bhavcopy():
    """Pass 5: previous trading day's NSE/BSE bhavcopy close prices ->
    price_dayN for companies in their Day1-10 window (see bhavcopy_sync.py),
    then the bounded gap-fill fallback for any (company, horizon) bhavcopy
    never had a row for. Same 'one bad source shouldn't stop the batch'
    resilience pattern as passes 1-4. Runs on its OWN daily job in
    start_scheduler() below (bhavcopy is only published once/day, well
    after this project's SYNC_INTERVAL_MINUTES cadence would re-check it
    usefully) -- same 'dedicated job + also reachable via run_sync_once()'
    pattern as sync_ipoji_open_ipos()/Pass 4.

    Returns {"sync": <run_bhavcopy_sync result or None>, "gap_fill":
    <backfill_price_gaps result or None>} -- None for a stage that raised,
    same "don't take down the batch" pattern as every other pass, but
    surfaced in the return value (like sync_ipoji_open_ipos()) rather than
    silently swallowed, so /api/sync/bhavcopy has real data to hand back
    instead of just a status string."""
    sync_result = None
    try:
        sync_result = run_bhavcopy_sync()
        logger.info("Bhavcopy price sync: %s", sync_result)
    except Exception as e:  # noqa: BLE001 -- same pattern as passes 1-4
        logger.warning("Bhavcopy price sync failed: %s", e)

    gap_result = None
    try:
        gap_result = backfill_price_gaps()
        logger.info("Bhavcopy gap-fill: %s", gap_result)
    except Exception as e:  # noqa: BLE001
        logger.warning("Bhavcopy gap-fill failed: %s", e)

    # Trajectory-prediction save hook: recompute + persist a fresh
    # trajectory prediction for exactly the companies that got a NEW
    # price_dayN cell this cycle -- from either the main sync or the
    # gap-fill fallback -- so the frontend can switch to reading the
    # last-saved row instead of computing on request. Same "one bad
    # company shouldn't stop the batch" resilience convention as every
    # other pass in this module.
    names = set((sync_result or {}).get("updated_companies", []))
    names |= set((gap_result or {}).get("filled_companies", []))
    saved, skipped, failed = 0, 0, 0
    if names:
        conn = db.get_connection()
        try:
            for name in sorted(names):
                # Canonicalize to the exact company_name the row is
                # actually stored under -- save_trajectory_prediction()
                # is a straight lookup-by-name call into
                # predict_trajectory_smart_for_company(), so a mismatched
                # name here would either miss the row entirely or (worse)
                # save under a second, slightly different name than the
                # one the read path looks up.
                record, exact = db.find_company(name)
                if record is None:
                    skipped += 1
                    logger.warning(
                        "Trajectory save hook: %r updated by bhavcopy but "
                        "find_company() couldn't resolve it -- skipping.", name,
                    )
                    continue
                try:
                    save_trajectory_prediction(conn, record.company_name)
                    saved += 1
                except TrajectoryPredictionError as e:
                    failed += 1
                    logger.warning(
                        "Trajectory save hook: prediction failed for %r: %s",
                        record.company_name, e,
                    )
        finally:
            conn.close()
    trajectory_result = {"saved": saved, "skipped_unresolved": skipped, "failed": failed}
    logger.info("Bhavcopy-triggered trajectory saves: %s", trajectory_result)

    return {"sync": sync_result, "gap_fill": gap_result, "trajectory_saves": trajectory_result}


def run_sync_once():
    # FIX (2026-08-15): sync_ipoji_open_ipos() now runs FIRST -- it's the
    # one that actually scrapes ipoji.com and populates ipo_live_tracker.
    # sync_active_ipos() (below) only reads that table now; it needs this
    # pass to have run first in the same cycle, or it'll find nothing.
    # FIX (2026-08-17): sync_recent_listings() (Pass 2) removed from the
    # automatic cadence -- see that function's updated docstring.
    # bhavcopy_sync() (Pass 5) now covers the same "fill price_dayN for
    # recently-listed companies" job, for free and in bulk, once/day.
    sync_ipoji_open_ipos()
    sync_active_ipos()
    sync_gmp_trend()
    sync_bhavcopy()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_sync_once, "interval", minutes=config.SYNC_INTERVAL_MINUTES,
                       id="ipo_sync", next_run_time=None)
    # Separate hourly job for the IPO Ji poll (Step 3's explicit ask), on
    # the SAME scheduler instance -- not a second BackgroundScheduler --
    # so both jobs share one thread pool and stop together on shutdown.
    # sync_ipoji_open_ipos() is ALSO reachable via run_sync_once() above
    # (the manual /api/sync button), so this job id lets it run
    # independently of whatever SYNC_INTERVAL_MINUTES happens to be.
    scheduler.add_job(sync_ipoji_open_ipos, "interval", hours=1,
                       id="ipoji_sync", next_run_time=None)
    # Bhavcopy is published once/day (see bhavcopy_sync.py) -- its own
    # daily job, same "dedicated cadence + also reachable via the manual
    # /api/sync button through run_sync_once()" pattern as ipoji_sync
    # above. NOTE: this fires at whatever wall-clock time the process
    # started + 24h multiples (APScheduler "interval", not "cron") -- swap
    # to a cron-style trigger with an explicit IST hour once NSE's real
    # bhavcopy publish time is confirmed (see bhavcopy_sync.py module
    # docstring point (a) and item 4's note on checking the real URL).
    scheduler.add_job(sync_bhavcopy, "interval", hours=24,
                       id="bhavcopy_sync", next_run_time=None)
    scheduler.start()
    logger.info(
        "Background sync started: full sync every %s minutes, IPO Ji poll every hour, "
        "bhavcopy sync every 24 hours.",
        config.SYNC_INTERVAL_MINUTES,
    )
    return scheduler


# --- Serverless note ---
# Vercel/Render-serverless functions spin down between requests, so a
# BackgroundScheduler started in start_scheduler() won't reliably tick in
# the background there. On those hosts, skip start_scheduler() and instead
# hit POST /api/sync (see main.py) from an external cron -- Vercel Cron,
# GitHub Actions on a schedule, or cron-job.org -- on the same
# SYNC_INTERVAL_MINUTES cadence. On a normal long-running host (Render web
# service, a VM, Railway, etc.) start_scheduler() at app startup works fine.
#
# gmp_sync's ipowatch source is intentionally NOT part of this cadence (see
# sync_gmp_trend() above) -- call POST /api/sync/gmp directly for that,
# with a small ipowatch_limit, from its own separate cron entry once you've
# confirmed it works, rather than folding it into SYNC_INTERVAL_MINUTES.
#
# The IPO Ji poll (sync_ipoji_open_ipos) runs as its OWN hourly job here,
# separate from SYNC_INTERVAL_MINUTES -- see start_scheduler() above. On a
# serverless host, a single external cron hitting POST /api/sync on
# SYNC_INTERVAL_MINUTES will therefore run the IPO Ji poll at that SAME
# cadence too (run_sync_once() calls it), not genuinely hourly, unless
# SYNC_INTERVAL_MINUTES already happens to be 60. If the two need to
# diverge on a serverless deployment, add a second external cron entry
# calling POST /api/sync on its own hourly schedule -- the route is
# idempotent-safe to call more often than needed (see
# sync_ipoji_open_ipos()'s docstring on repeat-poll safety).
