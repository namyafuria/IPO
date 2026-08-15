import logging
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ipo_tool.main")

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
    """
    src_tuple = tuple(s.strip() for s in sources.split(",") if s.strip())
    return run_gmp_sync(sources=src_tuple, ipowatch_limit=ipowatch_limit)


@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    """Manually kick the same sync the background scheduler runs -- this is
    the endpoint an external cron (Vercel Cron / GitHub Actions / etc.)
    should call on a serverless deployment, since a serverless function
    can't run its own persistent background loop. See scheduler.py.

    FIX (2026-08-15): run_sync_once() now runs as a background task rather
    than being awaited here -- it includes sync_ipoji_open_ipos(), which
    does 3 page-fetches x a 1.5s+ delay per currently-open IPO (~40
    companies -> 100+ sequential requests, 3+ minutes). Blocking this
    request on that made every caller (including an external cron with
    its own timeout) wait for the full run. Same reasoning as
    /api/sync_and_predict's step 1 above."""
    from .scheduler import run_sync_once
    background_tasks.add_task(run_sync_once)
    return {"status": "sync triggered (running in background)"}


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
    background_tasks: BackgroundTasks,
    sources: Optional[str] = "ipogyani",
    track_days: Optional[int] = None,
    include_trajectory: bool = True,
):
    """One-shot 'refresh live data, then predict everything currently
    relevant' -- runs server-side against the live deployment's DB, not a
    local copy. This is what the frontend's global refresh action (or a
    cron job) should call, rather than /api/sync/gmp alone.

    Steps, in order:
      1. sync_ipoji_open_ipos() is kicked off as a BACKGROUND task -- FIX
         (2026-08-15): this used to run synchronously, first thing, in
         this same request. That's 3 page-fetches x a 1.5s+ delay PER
         OPEN IPO (see ipoji.py's fetch_and_parse_ipo()/DELAY_SECONDS) --
         with ~40 companies currently open, that's 100+ sequential
         requests and 3+ minutes of blocking work on every single
         refresh click, which blows straight through the frontend's
         request timeout ("API is taking a while to respond..."). It's
         now fire-and-forget: this request returns using whatever
         ipo_live_tracker already has (from the last successful poll --
         either the hourly background job, if RUN_SCHEDULER=1, or a
         previous call to this same route), while the fresh scrape runs
         after the response is sent and will be picked up by the NEXT
         refresh. ipoji.py's per-company commit (see
         poll_and_save_open_ipos()'s own fix-log note) means a slow or
         interrupted background poll still leaves each company's data
         usable as it completes, rather than all-or-nothing.
      2. sync_active_ipos() -- reads the (possibly still-being-updated)
         ipo_live_tracker synchronously; this is just a DB read plus one
         Indian-API call per company, not a scrape, so it stays in the
         request path. Writes open_date/close_date/subscription_total/
         issue_category/etc. into ipo_master_records, which is what
         find_live_and_recent_companies() below actually filters on.
      3. run_gmp_sync(sources) -- refreshes gmp_trend and, via the fixed
         backfill step, ipo_master_records.gmp_percent (see gmp_sync.py).
      4. Finds every company currently open for bidding, or listed within
         the last `track_days` days (defaults to config.POST_LISTING_TRACK_DAYS).
      5. Calls predict_for_company() and, if include_trajectory, also
         predict_trajectory_for_company() for each -- with NO
         subscription/gmp override, so both read whatever the sync step
         just wrote into the DB.

    `sources`: same comma-separated ipogyani/ipowatch list as /api/sync/gmp
    -- this only controls run_gmp_sync's gmp_trend refresh (step 3); it has
    no effect on step 1/2, which are always ipoji now regardless of this
    param. Defaults to ipogyani only here (ipowatch is slow -- see that
    route's docstring -- so it's opt-in for this combined call rather than
    default).

    Returns: {"sync": <run_gmp_sync result>, "predictions": [ {company_name,
    gain, trajectory, error}, ... ]}. A per-company `error` field (instead
    of raising) means that one company's prediction failed -- the rest of
    the batch still returns. Note the response no longer waits on the
    ipoji poll (see step 1) -- if ipo_live_tracker was still empty/stale
    going into this call, `predictions` may be thin on this call and
    fuller on the next one, once the background poll has landed.
    """
    # Local import -- keeps scheduler.py (and its apscheduler dependency)
    # lazy-loaded, same pattern as /api/sync below, rather than a hard
    # module-level import that every request to this file would then need.
    from .scheduler import sync_ipoji_open_ipos, sync_active_ipos
    background_tasks.add_task(sync_ipoji_open_ipos)
    sync_active_ipos()
    src_tuple = tuple(s.strip() for s in sources.split(",") if s.strip())
    sync_result = run_gmp_sync(sources=src_tuple)

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
