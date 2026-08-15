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

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from .predict_trajectory import (
    predict_trajectory_smart_for_company,
    TrajectoryPredictionError,
)

router = APIRouter()


@router.get("/api/predict_trajectory_smart/{name}")
def get_predict_trajectory_smart(
    name: str,
    # Query param names deliberately match "subscription"/"gmp", NOT
    # "subscription_override"/"gmp_override" -- every other endpoint in
    # api.js (getPrediction, getTrajectory, and this one) already sends
    # those two short names. Aliasing here (rather than renaming
    # api.js) keeps this consistent with the existing convention and
    # avoids repeating the exact silent-param-mismatch bug fixed in
    # project plan §73 (api.js sent subscription/gmp, the route expected
    # subscription_override/gmp_override -- FastAPI just silently treated
    # them as absent since both are Optional, no error, no override
    # ever applied).
    subscription_override: Optional[float] = Query(
        None, alias="subscription",
        description="Current live subscription multiple, for a still-open issue.",
    ),
    gmp_override: Optional[float] = Query(
        None, alias="gmp",
        description="Current live GMP percent.",
    ),
):
    try:
        return predict_trajectory_smart_for_company(
            name,
            subscription_override=subscription_override,
            gmp_override=gmp_override,
        )
    except TrajectoryPredictionError as e:
        message = str(e)
        if message.startswith("No match found for"):
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=422, detail=message)
