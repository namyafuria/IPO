"""
routers_trajectory.py -- exposes predict_trajectory_smart_for_company()
over HTTP.

Follows the same router-module pattern already used elsewhere in this
project (see routers_live.py, wired into main.py via
`from .routers_live import router as live_router` /
`app.include_router(live_router)`) -- do the same here:

    from .routers_trajectory import router as trajectory_router
    app.include_router(trajectory_router)

Route: GET /api/predict_trajectory_smart/{name}
  Query params (both optional):
    subscription_override: float -- current live multiple, for an issue
                            that's still open (mirrors predict_trajectory_
                            for_company's own subscription_override param)
    gmp_override: float -- current live GMP percent, same idea

Error mapping, matching the existing predict.py/predict_trajectory.py
"raise a domain exception, let the router translate it" pattern:
  - "No match found for ... in the database." -> 404
  - every other TrajectoryPredictionError message (not tagged to a
    category, no subscription figure yet, no model files, etc.) -> 422,
    since the company DOES exist but the request can't be served as
    given (bad/incomplete input), not "resource not found"
  - anything unexpected -> let FastAPI's default 500 handler take it;
    deliberately NOT caught here, so a real bug surfaces as a real 500
    in logs rather than being silently reshaped into a 422
"""

"""
routers_trajectory.py -- exposes the trajectory prediction over HTTP.

FIX (2026-08-18): this route no longer calls
predict_trajectory_smart_for_company() directly on every request. Prices
now arrive once/day via bhavcopy_sync.py, which computes and persists a
trajectory prediction right after each company's price_dayN lands (see
scheduler.py's sync_bhavcopy() trajectory-save hook) -- so the normal
request path here just reads that saved row via
get_latest_trajectory_prediction(), which is instant regardless of how
slow the underlying model computation is.

EXCEPTION: subscription_override/gmp_override (a still-open issue's live,
not-yet-final numbers) can never be pre-cached -- there's no daily job
that knows what live number the caller has in hand right now. So a
request that passes either override still computes live, same as before.
This is expected to be a rare, user-triggered case (checking "what if"
before the issue closes), not the routine path, so it's fine for it to
stay slow.

Route: GET /api/predict_trajectory_smart/{name}
  Query params (both optional, force a live compute when either is set):
    subscription_override: float
    gmp_override: float

Error mapping:
  - company not found in the DB -> 404
  - company found, but no saved prediction exists yet (freshly listed,
    hasn't hit a bhavcopy cycle yet) -> 422, "not yet computed" -- this
    is the same honest-empty-state decision as the pre_listing-mode
    freeze, not a fallback to an on-demand compute
  - every other TrajectoryPredictionError (only reachable via the
    override live-compute path now) -> 422
  - anything unexpected -> let FastAPI's default 500 handler take it
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from . import db
from .predict_trajectory import (
    predict_trajectory_smart_for_company,
    TrajectoryPredictionError,
)
from .trajectory_predictions_store import get_latest_trajectory_prediction

router = APIRouter()


@router.get("/api/predict_trajectory_smart/{name}")
def get_predict_trajectory_smart(
    name: str,
    subscription_override: Optional[float] = Query(
        None, alias="subscription",
        description="Current live subscription multiple, for a still-open issue. "
                     "Forces a live compute instead of reading the cached prediction.",
    ),
    gmp_override: Optional[float] = Query(
        None, alias="gmp",
        description="Current live GMP percent. Forces a live compute instead of "
                     "reading the cached prediction.",
    ),
):
    record, _exact = db.find_company(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No match found for {name!r} in the database.")

    # Override path -- can't be served from cache, same live-compute call
    # this route used to make unconditionally.
    if subscription_override is not None or gmp_override is not None:
        try:
            return predict_trajectory_smart_for_company(
                record.company_name,
                subscription_override=subscription_override,
                gmp_override=gmp_override,
            )
        except TrajectoryPredictionError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # Normal path -- read the last-saved row from the bhavcopy-triggered
    # hook. No live compute here even if this returns None: a freshly
    # listed company simply hasn't had a bhavcopy cycle save one yet, and
    # that's an honest "not yet computed" state, not an error worth
    # silently papering over with a slow on-demand fallback.
    conn = db.get_connection()
    try:
        cached = get_latest_trajectory_prediction(conn, record.company_name)
    finally:
        conn.close()

    if cached is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No trajectory prediction has been computed yet for {record.company_name!r} -- "
                "it will be filled in after the next daily bhavcopy sync."
            ),
        )
    return cached
