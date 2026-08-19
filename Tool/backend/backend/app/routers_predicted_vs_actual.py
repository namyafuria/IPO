"""
routers_predicted_vs_actual.py -- item 3: GET /ipos/{slug}/predicted-vs-actual

Reuses the already-cached prediction (which -- per the real payload checked
this session -- already contains both the prediction AND the real prices
under 'actual_outcome'), so this route does no new DB joins. It just reads
the cached row and does the comparison math.

Confirmed against the real db.py this session:
  - get_connection() already sets row_factory=sqlite3.Row itself, so this
    file doesn't need to set it again.
  - find_company(query) returns a (record, exact_match) TUPLE, not a bare
    name -- record is an IPORecord or None. The canonical company_name
    used everywhere else in the DB (and the same string
    save_trajectory_prediction()/get_latest_trajectory_prediction() key
    on) is record.company_name.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .trajectory_predictions_store import get_latest_trajectory_prediction
from .bucket_utils import bucket_contains
from . import db

router = APIRouter()


@router.get("/ipos/{slug}/predicted-vs-actual")
def predicted_vs_actual(slug: str):
    record, _exact_match = db.find_company(slug)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No company matching '{slug}'")
    company_name = record.company_name

    conn = db.get_connection()
    try:
        cached = get_latest_trajectory_prediction(conn, company_name)
    finally:
        conn.close()

    if cached is None:
        raise HTTPException(status_code=404, detail=f"No saved prediction for '{company_name}'")

    actual = cached.get("actual_outcome") or {}
    price_day1 = actual.get("price_day1")

    horizons_out = {}
    for horizon_key, horizon_data in (cached.get("horizons") or {}).items():
        price_dayN = actual.get(f"price_{horizon_key}")

        if price_day1 is None or price_dayN is None:
            horizons_out[horizon_key] = {
                "predicted_bucket": horizon_data.get("top_bucket"),
                "actual_pct": None,
                "correct": None,
                "status": "pending",  # not listed long enough yet, or price not backfilled
            }
            continue

        actual_pct = round((price_dayN - price_day1) / price_day1 * 100, 2)
        top_bucket = horizon_data.get("top_bucket")
        correct = bucket_contains(top_bucket, actual_pct)

        horizons_out[horizon_key] = {
            "predicted_bucket": top_bucket,
            "actual_pct": actual_pct,
            "correct": correct,
            "status": "resolved",
        }

    return {
        "company": company_name,
        "listing_date": actual.get("listing_date"),
        "horizons": horizons_out,
    }
