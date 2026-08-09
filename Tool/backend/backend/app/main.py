import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, live_fetch
from .db import find_company
from .predict import predict_for_company, PredictionError
from .predict_trajectory import predict_trajectory_for_company, TrajectoryPredictionError

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
):
    """Problem B: post-listing price-trajectory bucket prediction (day2/
    day3/day5/day10 relative to the day1 close) for a company already in
    the DB. Same DB-only lookup as /api/predict -- call GET
    /api/company/{name} first if the company might be new.

    Returns all 4 horizons in one call, each flagged with `reliable` /
    `reliability_note`: validated training only clearly beat the naive
    baseline for Mainboard day2/day3/day5. Mainboard day10 and every SME
    horizon did NOT, and are returned as low-confidence/exploratory --
    the UI should surface that caveat rather than presenting all 4
    horizons with equal confidence.
    """
    try:
        return predict_trajectory_for_company(name, subscription_override=subscription)
    except TrajectoryPredictionError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/sync")
def trigger_sync():
    """Manually kick the same sync the background scheduler runs -- this is
    the endpoint an external cron (Vercel Cron / GitHub Actions / etc.)
    should call on a serverless deployment, since a serverless function
    can't run its own persistent background loop. See scheduler.py."""
    from .scheduler import run_sync_once
    run_sync_once()
    return {"status": "sync complete"}
