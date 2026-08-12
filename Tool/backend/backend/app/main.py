import logging
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, live_fetch
from .db import find_company, find_live_and_recent_companies
from .predict import predict_for_company, PredictionError
from .predict_trajectory import predict_trajectory_for_company, TrajectoryPredictionError
from .predict_trajectory_rolling import predict_trajectory_rolling
from .gmp_sync import run_gmp_sync

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
def trigger_sync():
    """Manually kick the same sync the background scheduler runs -- this is
    the endpoint an external cron (Vercel Cron / GitHub Actions / etc.)
    should call on a serverless deployment, since a serverless function
    can't run its own persistent background loop. See scheduler.py."""
    from .scheduler import run_sync_once
    run_sync_once()
    return {"status": "sync complete"}


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
      1. run_gmp_sync(sources) -- refreshes gmp_trend and, via the fixed
         backfill step, ipo_master_records.gmp_percent (see gmp_sync.py).
      2. Finds every company currently open for bidding, or listed within
         the last `track_days` days (defaults to config.POST_LISTING_TRACK_DAYS).
      3. Calls predict_for_company() and, if include_trajectory, also
         predict_trajectory_for_company() for each -- with NO
         subscription/gmp override, so both read whatever the sync step
         just wrote into the DB.

    `sources`: same comma-separated ipogyani/ipowatch list as /api/sync/gmp.
    Defaults to ipogyani only here (ipowatch is slow -- see that route's
    docstring -- so it's opt-in for this combined call rather than default).

    Returns: {"sync": <run_gmp_sync result>, "predictions": [ {company_name,
    gain, trajectory, error}, ... ]}. A per-company `error` field (instead
    of raising) means that one company's prediction failed -- the rest of
    the batch still returns.
    """
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
