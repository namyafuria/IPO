"""
Background sync -- periodically refreshes:
  1. Every IPO Guru "active" IPO (open/upcoming/recently listed), so the DB
     stays current on subscription/GMP without anyone having to search for it.
  2. Every DB row still within POST_LISTING_TRACK_DAYS of its listing_date,
     so price_day2/3/5/10 and current_price fill in as the days pass.

Runs in-process via APScheduler. On a serverless host (Vercel functions),
in-process background loops don't persist between invocations -- see the
note at the bottom for that case.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, db, live_fetch
from .fetchers import ipoguru

logger = logging.getLogger("ipo_tool.scheduler")


def sync_active_ipos():
    """Pass 1: everything IPO Guru currently considers active."""
    try:
        active = ipoguru.fetch_active_ipos()
    except ipoguru.IPOGuruError as e:
        logger.warning("Could not fetch active IPO list: %s", e)
        return
    for ipo in active:
        name = ipo.get("name")
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


def run_sync_once():
    sync_active_ipos()
    sync_recent_listings()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_sync_once, "interval", minutes=config.SYNC_INTERVAL_MINUTES,
                       id="ipo_sync", next_run_time=None)
    scheduler.start()
    logger.info("Background sync started, every %s minutes.", config.SYNC_INTERVAL_MINUTES)
    return scheduler


# --- Serverless note ---
# Vercel/Render-serverless functions spin down between requests, so a
# BackgroundScheduler started in start_scheduler() won't reliably tick in
# the background there. On those hosts, skip start_scheduler() and instead
# hit POST /api/sync (see main.py) from an external cron -- Vercel Cron,
# GitHub Actions on a schedule, or cron-job.org -- on the same
# SYNC_INTERVAL_MINUTES cadence. On a normal long-running host (Render web
# service, a VM, Railway, etc.) start_scheduler() at app startup works fine.
