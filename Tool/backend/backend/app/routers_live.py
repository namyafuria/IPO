"""
routers_live.py — Step 6: read-only endpoints serving the live-poller data
built in Steps 1-5.

  GET /ipos/open
      List of all currently-tracked-open IPOs (from ipo_live_tracker),
      each with its most recent live prediction attached (from
      live_predictions) if one exists yet.

  GET /ipos/{company_name}/live-history
      Full day-wise GMP + subscription history for one company (from
      gmp_trend + subscription_daywise), plus its full prediction history
      (every live_predictions row for that company, not just the latest --
      lets the frontend show how the prediction moved through the day/IPO
      window, consistent with the project's versioning-not-overwriting
      approach elsewhere).

ASSUMPTION FLAGGED, NOT DECIDED SILENTLY: the original 9-step plan named
this second endpoint "/ipos/{slug}/live-history". Nothing in the DB stores
an ipoji slug -- ipo_live_tracker/gmp_trend/subscription_daywise are all
keyed by company_name. Built against company_name (URL-encoded) instead.
If slug-based URLs are wanted later, that needs the poller itself to start
writing a slug column somewhere -- a separate decision, not made here.

Import path assumption: written as a sibling module to db.py/config.py/
predict.py inside the `app` package, same convention predict.py already
uses (`from .db import find_company`). Wire into main.py with:
    from .routers_live import router as live_router
    app.include_router(live_router)
If the actual package layout differs, only the two `from .` import lines
below need adjusting.
"""

import json
import sqlite3
from datetime import date, datetime
from typing import Optional

import pandas_market_calendars as mcal
from fastapi import APIRouter, HTTPException

from . import config

router = APIRouter()

# Shared NSE trading-calendar instance -- correctly skips weekends AND
# actual NSE holidays (verified against real 2026 sessions), which is why
# this is used instead of a plain "listing_date + 10 calendar days" check.
# Step 9 requirement: a listed company should keep showing in /ipos/listed
# until trading day 10 has actually elapsed, not 10 calendar days.
_NSE = mcal.get_calendar("NSE")


def _trading_days_elapsed(listing_date_str: str, as_of: Optional[date] = None) -> Optional[int]:
    """Counts NSE trading sessions from (and including) listing_date through
    (and including) as_of. Returns 1 on listing day itself, matching the
    project's existing price_day1 = listing-day-close convention (so this
    number lines up directly with the price_dayN / horizon columns).
    Returns None if listing_date is missing/unparseable, or 0 if the
    listing date is still in the future (shouldn't normally happen here
    since this only gets called on already-listed rows)."""
    if not listing_date_str:
        return None
    try:
        listing_dt = datetime.strptime(listing_date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    as_of = as_of or date.today()
    if listing_dt > as_of:
        return 0
    sessions = _NSE.valid_days(start_date=listing_dt, end_date=as_of)
    return len(sessions)


def _get_conn() -> sqlite3.Connection:
    # timeout=30: wait up to 30s for a lock to clear instead of raising
    # "database is locked" immediately -- the scheduler and every API
    # request open separate connections against the same file, so brief
    # contention during a scheduler write is expected and should be
    # waited out, not surfaced as a failure. WAL mode lets reads proceed
    # concurrently with a writer (only writer-vs-writer still blocks),
    # which is the actual fix for read endpoints like these stalling
    # behind scheduler writes.
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row is not None else None


@router.get("/ipos/open")
def get_open_ipos():
    """Every row in ipo_live_tracker that's still genuinely open for
    bidding, each with its latest live_predictions row attached (None if
    no prediction has been made for it yet -- e.g. subscription data
    hasn't come in on day 1 of bidding).

    FIX (2026-08-16): ipo_live_tracker previously included companies whose
    close_date had already passed -- ipoji.py's upsert_live_tracker()
    hardcodes status='open' for every company discovered on ipoji.com's
    "current-ipo" pages, which cover the WHOLE pre-listing lifecycle
    (actively bidding, closed-awaiting-allotment, upcoming), not just
    companies actually taking bids right now. The pruning step (removing a
    company once it drops off ipoji's current-ipo pages entirely) was
    already working correctly -- this is a separate gap: a company that's
    closed but not yet listed still legitimately appears on those pages,
    so it never gets pruned, yet showing it as "open for bidding" was
    misleading. Filtered here (read-time) rather than in the scraper, so
    the raw ipo_live_tracker data -- including close_date -- stays
    available for other consumers that might want the full pre-listing
    set, not just the actively-open subset this endpoint is named for."""
    today = date.today().isoformat()
    conn = _get_conn()
    try:
        trackers = conn.execute(
            """SELECT * FROM ipo_live_tracker
               WHERE close_date IS NULL OR close_date = '' OR close_date >= ?
               ORDER BY as_of DESC""",
            (today,),
        ).fetchall()

        out = []
        for t in trackers:
            company = t["company_name"]
            latest_pred = conn.execute(
                """SELECT * FROM live_predictions
                   WHERE company_name = ?
                   ORDER BY predicted_at DESC LIMIT 1""",
                (company,),
            ).fetchone()

            pred_dict = _row_to_dict(latest_pred)
            if pred_dict and pred_dict.get("bucket_probabilities"):
                pred_dict["bucket_probabilities"] = json.loads(pred_dict["bucket_probabilities"])

            out.append({
                "company_name": company,
                "issue_category": t["issue_category"],
                "sector": t["sector"],
                "status": t["status"],
                "open_date": t["open_date"],
                "close_date": t["close_date"],
                "price_band_upper": t["price_band_upper"],
                "issue_size_cr": t["issue_size_cr"],
                "current_subscription_total": t["current_subscription_total"],
                "current_subscription_qib": t["current_subscription_qib"],
                "current_subscription_hni": t["current_subscription_hni"],
                "current_subscription_rii": t["current_subscription_rii"],
                "current_gmp_percent": t["current_gmp_percent"],
                "as_of": t["as_of"],
                "latest_prediction": pred_dict,
            })
        return {"count": len(out), "ipos": out}
    finally:
        conn.close()


@router.get("/ipos/{company_name}/live-history")
def get_live_history(company_name: str):
    """Day-wise GMP + subscription history, plus the full prediction
    history (every poll's prediction, not just the latest), for one
    company. 404s if the company has no live-tracking data at all --
    distinct from "tracked but no predictions yet", which returns empty
    prediction_history rather than a 404."""
    conn = _get_conn()
    try:
        tracker = conn.execute(
            "SELECT * FROM ipo_live_tracker WHERE company_name = ?", (company_name,)
        ).fetchone()
        gmp_rows = conn.execute(
            "SELECT * FROM gmp_trend WHERE company_name = ? ORDER BY gmp_date", (company_name,)
        ).fetchall()
        sub_rows = conn.execute(
            "SELECT * FROM subscription_daywise WHERE company_name = ? ORDER BY day_number", (company_name,)
        ).fetchall()
        pred_rows = conn.execute(
            "SELECT * FROM live_predictions WHERE company_name = ? ORDER BY predicted_at", (company_name,)
        ).fetchall()

        if tracker is None and not gmp_rows and not sub_rows:
            raise HTTPException(status_code=404, detail=f"No live-tracking data found for '{company_name}'.")

        predictions = []
        for p in pred_rows:
            d = _row_to_dict(p)
            if d.get("bucket_probabilities"):
                d["bucket_probabilities"] = json.loads(d["bucket_probabilities"])
            predictions.append(d)

        return {
            "company_name": company_name,
            "tracker": _row_to_dict(tracker),
            "gmp_history": [_row_to_dict(r) for r in gmp_rows],
            "subscription_history": [_row_to_dict(r) for r in sub_rows],
            "prediction_history": predictions,
        }
    finally:
        conn.close()


@router.get("/ipos/listed")
def get_listed_ipos():
    """Step 9: every company still inside its Day1-10 trajectory window --
    i.e. it has listed (listing_date <= today) but trading day 10 hasn't
    happened yet -- so its Problem B prediction is still live/relevant.
    Once trading day 10 elapses, a company drops out of this list; it's
    still reachable via normal search, per the project's decision that a
    completed trajectory doesn't need its own tracked view.

    Deliberately DB-only and lightweight (no live_fetch, no trajectory
    computation here) -- this just answers "who belongs in the Listed
    section right now". The frontend calls the existing
    /api/predict_trajectory_smart/{name} endpoint per company for the
    actual compact prediction shown on each card, reusing the same
    single source of truth as the search page's TrajectoryPanel instead
    of duplicating that logic here.

    "Day 10" is counted in real NSE trading sessions (see
    _trading_days_elapsed), not calendar days -- a company that listed
    last Friday is still on trading-day 2 by Monday, not day 4.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT company_name, listing_date, issue_category, sector,
                      subscription_total, gmp_percent
               FROM ipo_master_records
               WHERE listing_date IS NOT NULL AND listing_date != ''"""
        ).fetchall()

        out = []
        for r in rows:
            elapsed = _trading_days_elapsed(r["listing_date"])
            if elapsed is None or elapsed < 1 or elapsed >= 10:
                continue
            out.append({
                "company_name": r["company_name"],
                "listing_date": r["listing_date"],
                "issue_category": r["issue_category"],
                "sector": r["sector"],
                "subscription_total": r["subscription_total"],
                "gmp_percent": r["gmp_percent"],
                "trading_days_elapsed": elapsed,
            })

        out.sort(key=lambda x: x["trading_days_elapsed"])
        return {"count": len(out), "ipos": out}
    finally:
        conn.close()
