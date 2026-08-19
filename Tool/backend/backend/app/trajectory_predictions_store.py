"""
trajectory_predictions_store.py -- caching layer for Problem B (trajectory)
predictions, so /api/predict_trajectory_smart/{name} can serve a saved
result instead of computing on every request.

Mirrors the pattern already established for Problem A in live_predict.py's
live_predictions table: insert-only, never overwrite a prior row, latest
row (ORDER BY computed_at DESC LIMIT 1) is what gets served. Same reasoning
carries over here -- keeps a history of how the prediction moved (e.g. from
pre_listing mode to rolling mode once price_day2/price_day5 land via
bhavcopy), consistent with the project's existing "version, don't
overwrite" decision.

Unlike live_predictions (which stores flat model-selection columns because
Problem A is a single bucket prediction), predict_trajectory_smart_for_company()
returns a nested per-horizon structure (day2/day3/day5/day10, each with its
own mode/buckets/validation stats) -- so the full result is stored as one
JSON blob (payload_json) rather than flattened into columns. A handful of
columns are pulled out alongside it purely so a caller can filter/sort
without deserializing every row: company_name, issue_category, computed_at.

Table is created with CREATE TABLE IF NOT EXISTS on first use here, same
convention live_predict.py's __main__ block uses for live_predictions --
no separate migration file in this project for that table, so none is
introduced for this one either.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .predict_trajectory import (
    predict_trajectory_smart_for_company,
    TrajectoryPredictionError,
)

_TABLE_READY = False


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotent -- cheap enough to call on every save/read, but guarded
    with a module-level flag so a hot path (every cache read) doesn't
    re-run CREATE TABLE IF NOT EXISTS every single call."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trajectory_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            issue_category TEXT,
            payload_json TEXT NOT NULL,
            computed_at TEXT NOT NULL
        )"""
    )
    # Every read filters on company_name then sorts by computed_at DESC --
    # index both together so "latest row for this company" stays a cheap
    # index lookup even once the table has months of history in it.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_trajectory_predictions_company_computed
           ON trajectory_predictions (company_name, computed_at DESC)"""
    )
    conn.commit()
    _TABLE_READY = True


def save_trajectory_prediction(
    conn: sqlite3.Connection,
    company_name: str,
    subscription_override: Optional[float] = None,
    gmp_override: Optional[float] = None,
) -> dict:
    """Computes predict_trajectory_smart_for_company() for `company_name`
    and persists the full result as a new row. Raises
    TrajectoryPredictionError unchanged (same cases as the underlying
    function -- company not found, no category, no subscription figure,
    no model files) so callers (the scheduler pass, or a manual trigger)
    handle it the same way they already handle that exception elsewhere.

    subscription_override/gmp_override are accepted here too (rather than
    only in the read path) so a caller CAN persist an override-based
    prediction if it ever wants to -- not used by the scheduler pass,
    which always calls this with no overrides (DB values only, which is
    what should be cached)."""
    result = predict_trajectory_smart_for_company(
        company_name,
        subscription_override=subscription_override,
        gmp_override=gmp_override,
    )

    _ensure_table(conn)
    conn.execute(
        """INSERT INTO trajectory_predictions
           (company_name, issue_category, payload_json, computed_at)
           VALUES (?, ?, ?, ?)""",
        (
            result["company_name"],
            result["issue_category"],
            json.dumps(result),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return result


def get_latest_trajectory_prediction(
    conn: sqlite3.Connection, company_name: str
) -> Optional[dict]:
    """Returns the most recently saved prediction payload for this exact
    company_name (same string db.find_company() resolved to when the row
    was saved -- see the route's job of resolving name -> canonical
    company_name before calling this, in the next step), or None if
    nothing's been cached yet."""
    _ensure_table(conn)
    row = conn.execute(
        """SELECT payload_json FROM trajectory_predictions
           WHERE company_name = ?
           ORDER BY computed_at DESC LIMIT 1""",
        (company_name,),
    ).fetchone()
    if row is None:
        return None
    payload = row["payload_json"] if isinstance(row, sqlite3.Row) else row[0]
    return json.loads(payload)
