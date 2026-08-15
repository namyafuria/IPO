"""
Background sync -- periodically refreshes:
  1. Every ipogyani "active" IPO (open/awaiting-allotment/upcoming), so the
     DB stays current on subscription/GMP/dates without anyone having to
     search for it. (Was IPO Guru until 2026-08-12 -- see sync_active_ipos().)
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
from .gmp_sync import run_gmp_sync, _ipogyani_fetch_live_status
from .fetchers import ipoji

logger = logging.getLogger("ipo_tool.scheduler")


def sync_active_ipos():
    """Pass 1: everything ipogyani's /live-ipo page currently considers
    active (open, awaiting allotment/listing, or upcoming).

    FIX (2026-08-12): was ipoguru.fetch_active_ipos(), which has been
    failing silently on every call since IPOGURU_API_KEY was never set on
    Render -- see gmp_sync.py's _ipogyani_fetch_live_status() docstring.
    Swapped to that function instead. Each call already goes through
    live_fetch.fetch_and_upsert(), which merges in price_band/gmp_percent
    from this same source -- so this pass now also writes real
    open_date/close_date/allotment_date/listing_date into
    ipo_master_records, which is what find_live_and_recent_companies()
    (the "LIVE IPOS" tab) actually reads. issue_category still isn't set
    by this path directly; see live_fetch.py's own fix-log note on that."""
    try:
        active = _ipogyani_fetch_live_status()
    except Exception as e:  # noqa: BLE001 -- one bad source call shouldn't stop the batch
        logger.warning("Could not fetch active IPO list from ipogyani: %s", e)
        return
    if not active:
        # gmp_sync.py's _ipogyani_fetch_live_status() logs its own detailed
        # warning distinguishing "0 cards found at all" vs "cards found but
        # none matched" -- check ipo_tool.gmp_sync log lines for the real
        # cause. This line just confirms the pass genuinely got nothing,
        # rather than skipped writes looking identical to "no companies live".
        logger.warning("ipogyani active-IPO list came back empty -- see ipo_tool.gmp_sync warnings above for why.")
        return
    for ipo in active:
        name = ipo.get("company_name")
        if not name:
            continue
        try:
            live_fetch.fetch_and_upsert(name)
        except Exception as e:  # noqa: BLE001 -- one bad company shouldn't stop the batch
            logger.warning("Sync failed for %r: %s", name, e)


def sync_recent_listings():
    """Pass 2: DB rows that listed recently enough to still need price_dayN
    filled in, but that IPO Guru's 'active' list may have already dropped."""
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


def run_sync_once():
    sync_active_ipos()
    sync_recent_listings()
    sync_gmp_trend()
    sync_ipoji_open_ipos()


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
    scheduler.start()
    logger.info(
        "Background sync started: full sync every %s minutes, IPO Ji poll every hour.",
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
