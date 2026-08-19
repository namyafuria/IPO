import logging
import sqlite3
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, live_fetch
from .db import find_company, find_live_and_recent_companies
from .predict import predict_for_company, PredictionError
from .predict_trajectory import (
    predict_trajectory_for_company,
    predict_trajectory_smart_for_company,
    TrajectoryPredictionError,
)
from .predict_trajectory_rolling import predict_trajectory_rolling
from .gmp_sync import run_gmp_sync
from .routers_trajectory import router as trajectory_router
from .routers_live import router as live_router
from .routers_predicted_vs_actual import router as predicted_vs_actual_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ipo_tool.main")

# FIX (2026-08-16): a full sync (run_sync_once -- polls every open IPO on
# ipoji.com, multiple page-fetches per company plus rate-limit delays) can
# easily take several minutes. If cron-job.org's interval is shorter than
# that, a second /api/sync call would previously start running WHILE the
# first was still mid-sync -- two threads writing to the same SQLite file
# at once, which is exactly what caused the "database is locked" errors in
# production (confirmed via Render logs 2026-08-16). This lock makes a
# second overlapping call a cheap no-op (returns immediately, doesn't
# queue/block) instead of racing the first one's writes. Non-blocking
# acquire on purpose -- an external cron doesn't need to wait; it just
# needs to not step on the sync already in progress.
_sync_lock = threading.Lock()

app = FastAPI(title="IPO Analyser API")

# Wide open for now -- tighten to your actual frontend domain once deployed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(trajectory_router)
app.include_router(live_router)
app.include_router(predicted_vs_actual_router)


@app.on_event("startup")
def _maybe_start_scheduler():
    """Only starts the in-process background sync if RUN_SCHEDULER=1 is set.
    Leave this unset on serverless hosts (Vercel) -- see scheduler.py's
    note -- and instead call POST /api/sync from an external cron. On a
    long-running host (Render web service, VM, etc.) set RUN_SCHEDULER=1."""
    import os
    if os.environ.get("RUN_SCHEDULER") == "1":
        from .scheduler import start_scheduler
        start_scheduler()


@app.get("/api/debug/status")
def debug_status():
    """Diagnostic endpoint -- reports actual DB/scheduler state directly,
    so 'are IPOs actually in the DB, and is anything keeping them fresh'
    can be checked with one request instead of reading Render logs.

    Added 2026-08-15 while chasing an empty-frontend bug that turned out
    to be about which process/schedule was populating the DB, not the
    application code -- this makes that state directly inspectable going
    forward instead of inferring it from log timestamps."""
    import os
    conn = None
    try:
        from . import db
        conn = db.get_connection()
        tracker_count = conn.execute("SELECT COUNT(*) AS c FROM ipo_live_tracker").fetchone()["c"]
        tracker_latest = conn.execute("SELECT MAX(as_of) AS m FROM ipo_live_tracker").fetchone()["m"]
        master_count = conn.execute("SELECT COUNT(*) AS c FROM ipo_master_records").fetchone()["c"]
        master_latest = conn.execute("SELECT MAX(last_updated) AS m FROM ipo_master_records").fetchone()["m"]
    finally:
        if conn is not None:
            conn.close()
    return {
        "ipo_live_tracker": {"row_count": tracker_count, "latest_as_of": tracker_latest},
        "ipo_master_records": {"row_count": master_count, "latest_last_updated": master_latest},
        "run_scheduler_env_set": os.environ.get("RUN_SCHEDULER") == "1",
        "render_git_commit": os.environ.get("RENDER_GIT_COMMIT"),
        "note": (
            "If ipo_live_tracker.row_count is 0, POST /api/sync has never "
            "completed successfully on this deployment yet -- it must be "
            "called (by you, or by an external cron) at least once, and "
            "then on a recurring schedule, for the frontend to show data. "
            "run_scheduler_env_set=false means nothing in-process will "
            "ever call it automatically -- you need an external cron "
            "(e.g. cron-job.org) hitting POST /api/sync every 10-15 min."
        ),
    }


@app.get("/api/health")
def health():
    missing = config.missing_keys()
    return {"status": "ok" if not missing else "degraded", "missing_env_vars": missing}


@app.get("/api/company/{name}")
def get_company(name: str):
    """Looks up a company. DB-first; if it's not in the DB at all, falls
    through to a live fetch (IPO Guru + Indian API) automatically -- so a
    search for a company we've never seen still returns an answer instead
    of a 404, per the 'just search a name and get a prediction' UI goal."""
    record, exact = find_company(name)
    if record is not None:
        return {"exact_match": exact, "source": "db", "record": record}

    try:
        fetched = live_fetch.fetch_and_upsert(name)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"No match found for '{name}' in the database or live sources.")
    return {"exact_match": True, "source": "live_fetch", "record": fetched}


@app.post("/api/company/{name}/refresh")
def refresh_company(name: str):
    """Explicit refresh button target -- re-fetches live data for a company
    regardless of what's already in the DB, and overwrites with anything
    new (existing fields are preserved where a source returns nothing new,
    per live_fetch._merge)."""
    try:
        fetched = live_fetch.fetch_and_upsert(name)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"No match found for '{name}' from any source.")
    return {"record": fetched}


@app.get("/api/predict/{name}")
def predict_company(
    name: str,
    subscription: Optional[float] = None,
    gmp: Optional[float] = None,
):
    """Bucket-probability prediction for a company already in the DB (does
    NOT live-fetch first -- call GET /api/company/{name} beforehand if the
    company might be new, so the DB has a row to predict from).

    Query params let the frontend override subscription/GMP for a still-open
    issue, same as predict_by_name.py's --subscription/--gmp flags: pass the
    live reading you have, get a PROVISIONAL-flagged prediction back. Both
    are marked provisional in `inputs_used` so the UI can show that caveat.
    """
    try:
        return predict_for_company(name, subscription_override=subscription, gmp_override=gmp)
    except PredictionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/predict_trajectory/{name}")
def predict_trajectory(
    name: str,
    subscription: Optional[float] = None,
    gmp: Optional[float] = None,
):
    """Problem B: post-listing price-trajectory bucket prediction (day2/
    day3/day5/day10 relative to the day1 close) for a company already in
    the DB. Same DB-only lookup as /api/predict -- call GET
    /api/company/{name} first if the company might be new.

    Query params: `subscription` and `gmp` (same plain names as
    /api/predict -- NOT `subscription_override`/`gmp_override`; that suffix
    only exists on the underlying Python function's keyword args). `gmp`
    only actually changes the output for Mainboard day5/day10 (the two
    horizons wired to the GMP-augmented model, per PREFER_GMP in
    predict_trajectory.py) -- for every other category/horizon it's
    accepted but has no effect on which model is selected.

    Returns all 4 horizons in one call, each flagged with `reliable` /
    `reliability_note`: validated training beat the naive baseline for
    Mainboard day2/day3/day5/day10 (day5/day10 via the GMP variant) and
    SME day2. SME day3/day5/day10 did NOT, and are returned as
    low-confidence/exploratory -- the UI should surface that caveat
    rather than presenting all 4 horizons with equal confidence.
    """
    try:
        return predict_trajectory_for_company(name, subscription_override=subscription, gmp_override=gmp)
    except TrajectoryPredictionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/predict_trajectory_rolling/{name}")
def predict_trajectory_rolling_route(name: str, horizon: str):
    """Problem B rolling/updating mode: predicts day5 or day10 using the
    ACTUAL observed early-day price move (day1->day2 for the day5 model,
    day1->day5 for the day10 model), instead of pre-listing features alone.
    Substantially more accurate than /api/predict_trajectory once that
    earlier day's price is known (see predict_trajectory_rolling.py's
    holdout numbers) -- but by definition only usable post-listing, once
    the relevant earlier day has actually elapsed.

    `horizon` is required and must be 'day5' or 'day10' (day2/day3 have no
    rolling variant; day1 has nothing to roll from).

    Returns 409 if the earlier day's actual price isn't in the DB yet for
    this company -- the frontend should only surface this as an "update
    with actual data" action once that's available, not show it always.
    Returns 404 if there's no rolling model for the company's category, or
    if the company itself isn't found.
    """
    if horizon not in ("day5", "day10"):
        raise HTTPException(status_code=400, detail="horizon must be 'day5' or 'day10'")

    record, exact = find_company(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No match found for '{name}' in the database.")

    known_col = "price_day2" if horizon == "day5" else "price_day5"
    known_price = getattr(record, known_col, None)
    if known_price is None:
        raise HTTPException(
            status_code=409,
            detail=f"{known_col} not yet available for '{record.company_name}' -- this endpoint "
                   "only works once that trading day has actually elapsed post-listing.",
        )

    result = predict_trajectory_rolling(
        category=record.issue_category,
        horizon=horizon,
        subscription_total=record.subscription_total,
        sector=record.sector,
        price_day1=record.price_day1,
        known_price=known_price,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No rolling model available for category '{record.issue_category}'.",
        )
    result["company_name"] = record.company_name
    result["exact_match"] = exact
    return result


@app.get("/api/predict_trajectory_smart/{name}")
def predict_trajectory_smart(
    name: str,
    subscription: Optional[float] = None,
    gmp: Optional[float] = None,
):
    """Problem B, per-horizon smart dispatch: for each of day2/day3/day5/
    day10, uses the rolling model if that horizon's prerequisite actual
    price is already in the DB (price_day2 for day5, price_day5 for
    day10), otherwise falls back to the pre-listing model. day2/day3
    always use the pre-listing model (no rolling variant exists).

    Unlike /api/predict_trajectory_rolling/{name}, this never 409s -- a
    horizon whose prerequisite data isn't ready yet is just silently
    served pre-listing. If the company has already listed but
    price_day1 isn't in the DB yet, this fetches it synchronously first
    rather than treating the company as still pre-listing.

    Each horizon in the response carries a "mode" field ("pre_listing" or
    "rolling") so the frontend can show which basis was used, instead of
    inferring it from which price fields are null.

    The existing /api/predict_trajectory_rolling/{name} route is left as-is
    for a frontend that wants to explicitly force/check rolling status;
    this is the new default the frontend should call instead.
    """
    try:
        return predict_trajectory_smart_for_company(name, subscription_override=subscription, gmp_override=gmp)
    except TrajectoryPredictionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/sync/gmp")
def trigger_gmp_sync(sources: Optional[str] = "ipogyani,ipowatch", ipowatch_limit: Optional[int] = None):
    """Runs the GMP scrapers directly and upserts into gmp_trend.

    `sources`: comma-separated, any of "ipogyani","ipowatch" (default both).
    `ipowatch_limit`: caps how many ipowatch pages get scraped this call --
    IMPORTANT on a serverless host: ipowatch's discovery step crawls the
    site's sitemap and can turn up hundreds of pages, which will likely
    exceed a serverless function's execution time limit if run uncapped in
    one request. Pass a small limit (e.g. 15) and call this repeatedly
    (e.g. via Vercel Cron every 10 minutes) rather than expecting one call
    to finish the whole site. On a long-running host (Render/VM with
    RUN_SCHEDULER=1) it's fine to leave ipowatch_limit unset.

    ipogyani has no such concern -- it only covers currently-live IPOs, a
    small, bounded set, so it's safe to run uncapped on any host.

    LOCKED (2026-08-16): shares _sync_lock with /api/sync and
    /api/sync_and_predict -- all three write to the same SQLite file, and
    the lock was previously only guarding /api/sync, which meant this
    route could still collide with a /api/sync run in progress and throw
    "database is locked" (confirmed happening in production 2026-08-16).
    Now any one of the three running blocks the other two from starting
    until it finishes, rather than just blocking duplicate calls to
    itself."""
    if not _sync_lock.acquire(blocking=False):
        logger.info("Another sync is already in progress -- skipping GMP sync trigger.")
        return {"status": "sync already in progress, skipped"}
    try:
        src_tuple = tuple(s.strip() for s in sources.split(",") if s.strip())
        return run_gmp_sync(sources=src_tuple, ipowatch_limit=ipowatch_limit)
    finally:
        _sync_lock.release()


@app.post("/api/sync/bhavcopy")
def trigger_bhavcopy_sync():
    """Runs the daily NSE/BSE bhavcopy price sync (fills price_day1/2/3/5/10
    for companies in their Day1-10 window from the previous trading day's
    EOD close) plus the bounded Indian-API gap-fill fallback for rows
    bhavcopy never got a row for -- see bhavcopy_sync.py.

    Bhavcopy is only published once per trading day, so this is meant to
    be called by its own once-daily external cron entry (cron-job.org),
    separate from /api/sync's 4-hour cron cadence -- see bhavcopy_sync.py's
    module docstring on confirming NSE's actual publish time before fixing
    that schedule. (The separate 10-min GET /ipos/open keep-alive ping some
    deployments use to stop Render's free tier from spinning down doesn't
    touch this route or live_fetch at all -- it's a read-only query against
    ipo_live_tracker, no Indian API involved.) It's idempotent-safe to call more than once a day
    regardless: run_bhavcopy_sync()/backfill_price_gaps() only ever fill a
    currently-NULL price_dayN cell, never overwrite one, so a repeat call
    the same day is just wasted work, not a correctness problem.

    LOCKED: shares _sync_lock with /api/sync, /api/sync/gmp, and
    /api/sync_and_predict -- same reasoning as those three (all write to
    the same SQLite file). Same non-blocking-skip style as /api/sync/gmp,
    not the 409 style /api/sync_and_predict uses -- this is a scheduled
    background sync route, not a request a frontend action is waiting on."""
    from .scheduler import sync_bhavcopy

    if not _sync_lock.acquire(blocking=False):
        logger.info("Another sync is already in progress -- skipping bhavcopy sync trigger.")
        return {"status": "sync already in progress, skipped"}
    try:
        result = sync_bhavcopy()
        return {"status": "bhavcopy sync complete", **result}
    finally:
        _sync_lock.release()


@app.post("/api/sync")
def trigger_sync():
    """Manually kick the same sync the background scheduler runs -- this is
    the endpoint an external cron (Vercel Cron / GitHub Actions /
    cron-job.org / etc.) should call, since a serverless OR free-tier host
    can't be trusted to keep a background task alive after a request
    finishes (see FIX LOG below). Deliberately BLOCKING -- an external
    cron can wait minutes for a 200, unlike a browser tab.

    FIX (2026-08-15): tried making this fire-and-forget via FastAPI
    BackgroundTasks. That doesn't work on Render's free tier: background
    tasks only start AFTER the HTTP response is sent, and Render can
    (and did, per production logs) spin the instance down as soon as the
    request/response cycle looks finished -- killing the in-progress
    ipoji scrape mid-run with no error logged, because the process itself
    was terminated, not the scrape. Blocking here means the request stays
    "in flight" for the whole duration, which is exactly what keeps the
    instance alive until the sync actually finishes.

    THIS ROUTE MUST BE CALLED ON A SCHEDULE BY SOMETHING EXTERNAL --
    cron-job.org hitting this URL every 10-15 minutes is the simplest
    option on Render's free tier. Nothing in this codebase calls this
    route automatically unless RUN_SCHEDULER=1 is set (see
    _maybe_start_scheduler() above) -- and even then, the free tier can
    still spin the instance down between the scheduler's own ticks if
    there's no incoming HTTP traffic, so an external cron hitting this
    URL is the only fully reliable option on this tier regardless.

    LOCKED (2026-08-16): a full sync can take longer than the cron
    interval, so if one is already in progress this returns immediately
    with a "skipped" status instead of starting a second overlapping run
    that would fight the first one for the SQLite write lock -- see
    _sync_lock's comment above the app definition."""
    from .scheduler import run_sync_once

    if not _sync_lock.acquire(blocking=False):
        logger.info("Sync already in progress -- skipping this trigger.")
        return {"status": "sync already in progress, skipped"}

    try:
        run_sync_once()
        return {"status": "sync complete"}
    finally:
        _sync_lock.release()


# ---------------------------------------------------------------------------
# NEW: sync + predict, in one server-side call.
#
# This is the piece that was previously a local-only script
# (fetch_live_and_predict.py) that never touched the deployed DB or API.
# Moved here so it runs ON the server against the live DB, using the real
# predict_for_company / predict_trajectory_for_company functions directly.
#
# The "which companies are currently live" lookup goes through
# db.find_live_and_recent_companies() (schema confirmed against schemas.py:
# open_date/close_date/listing_date/issue_category all real columns) so it
# shares the exact same DB_PATH/connection logic as find_company() and
# every other DB access in this project -- no separate raw sqlite3.connect()
# here that could silently point at a different file.
# ---------------------------------------------------------------------------
@app.post("/api/sync_and_predict")
def sync_and_predict(
    sources: Optional[str] = "ipogyani",
    track_days: Optional[int] = None,
    include_trajectory: bool = True,
):
    """One-shot 'refresh live data, then predict everything currently
    relevant' -- runs server-side against the live deployment's DB, not a
    local copy. This is what the frontend's global refresh action (or a
    cron job) should call, rather than /api/sync/gmp alone.

    Steps, in order:
      1. sync_active_ipos() -- a DB read (ipo_live_tracker) plus one
         Indian-API call per already-tracked company, NOT a scrape, so
         it's fast enough to run synchronously here. Writes open_date/
         close_date/subscription_total/issue_category/etc. into
         ipo_master_records, which is what find_live_and_recent_
         companies() below actually filters on.

         FIX (2026-08-15): this route used to also try to trigger
         sync_ipoji_open_ipos() itself (first synchronously, then via a
         FastAPI BackgroundTask) -- both failed for different reasons.
         Synchronously, it's 3 page-fetches x 1.5s+ delay per open IPO
         (~40 companies -> 100+ requests, 3+ minutes), blowing through
         the frontend's request timeout. As a BackgroundTask, it doesn't
         even start until AFTER this response is sent, and Render's free
         tier can spin the instance down as soon as the response looks
         complete -- silently killing the scrape mid-run with nothing
         logged, since the *process* gets terminated, not the task.
         There's no reliable way to run a multi-minute scrape inside a
         request/response cycle on this hosting tier.

         The fix is to not try: this route now ONLY reads whatever
         ipo_live_tracker / ipo_master_records already have. Keeping that
         data fresh is POST /api/sync's job -- see that route's docstring
         -- which MUST be called on a schedule by something external
         (e.g. cron-job.org every 10-15 min) for this route to ever
         return non-empty results on a free-tier deployment.
      2. run_gmp_sync(sources) -- refreshes gmp_trend and, via the fixed
         backfill step, ipo_master_records.gmp_percent (see gmp_sync.py).
      3. Finds every company currently open for bidding, or listed within
         the last `track_days` days (defaults to config.POST_LISTING_TRACK_DAYS).
      4. Calls predict_for_company() and, if include_trajectory, also
         predict_trajectory_for_company() for each -- with NO
         subscription/gmp override, so both read whatever the sync step
         just wrote into the DB.

    `sources`: same comma-separated ipogyani/ipowatch list as /api/sync/gmp
    (controls step 2 only).

    Returns: {"sync": <run_gmp_sync result>, "predictions": [ {company_name,
    gain, trajectory, error}, ... ]}. A per-company `error` field (instead
    of raising) means that one company's prediction failed -- the rest of
    the batch still returns. If `predictions` comes back empty or thin,
    that means POST /api/sync hasn't run recently enough -- check that
    it's wired up on an external schedule (see GET /api/debug/status
    below to check directly, without digging through logs).
    """
    # Local import -- keeps scheduler.py (and its apscheduler dependency)
    # lazy-loaded, same pattern as /api/sync below, rather than a hard
    # module-level import that every request to this file would then need.
    from .scheduler import sync_active_ipos

    # LOCKED (2026-08-16): shares _sync_lock with /api/sync and
    # /api/sync/gmp -- this route's sync_active_ipos()/run_gmp_sync() calls
    # write to the same SQLite file those do, and were previously
    # unguarded, which let this route collide with a /api/sync run in
    # progress and throw "database is locked" (confirmed happening in
    # production 2026-08-16, traced to the old frontend build's "Live
    # IPOs" tab calling this route while cron-job.org's /api/sync was
    # still mid-run). Only the writing portion is held under the lock --
    # the read-only prediction loop below runs after release, so a slow
    # prediction batch doesn't block other syncs longer than necessary.
    # If the lock is already held, this returns a 409 rather than silently
    # returning stale/empty predictions, since (unlike /api/sync) a
    # "skipped" sync here would otherwise look like a normal successful
    # response with just thin data.
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Another sync is already in progress -- try again shortly.",
        )
    try:
        sync_active_ipos()
        src_tuple = tuple(s.strip() for s in sources.split(",") if s.strip())
        sync_result = run_gmp_sync(sources=src_tuple)
    finally:
        _sync_lock.release()

    days = track_days if track_days is not None else config.POST_LISTING_TRACK_DAYS
    try:
        company_names = find_live_and_recent_companies(days)
    except sqlite3.OperationalError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not query live/recent companies: {e}",
        )

    predictions = []
    for name in company_names:
        entry = {"company_name": name}

        # Full live record (subscription, GMP, dates, etc.) so the frontend
        # can render everything in one response, same shape /api/company
        # already returns -- no extra per-company request needed.
        record, _ = find_company(name)
        entry["record"] = record.model_dump() if record is not None else None

        try:
            entry["gain"] = predict_for_company(name)
        except PredictionError as e:
            entry["gain_error"] = str(e)

        if include_trajectory:
            try:
                entry["trajectory"] = predict_trajectory_for_company(name)
            except TrajectoryPredictionError as e:
                entry["trajectory_error"] = str(e)

        predictions.append(entry)

    return {"sync": sync_result, "predictions": predictions}
