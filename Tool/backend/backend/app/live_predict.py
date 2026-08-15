"""
live_predict.py — Step 5: builds a live prediction for a currently-open IPO
using ipo_live_tracker's current snapshot + gmp_trend day-wise history run
through gmp_trend_features.py, then calls the appropriate bucket model.

Model selection logic (documented, not guessed):
  1. issue_category unknown/missing        -> PredictionError (can't pick a model)
  2. gmp_percent not available yet          -> fall back to plain v13/v7 (no GMP
                                                at all, i.e. before the IPO even
                                                has a GMP quote)
  3. gmp_percent available, trend features
     all NaN (e.g. only 1 snapshot so far)  -> use v14_gmp anyway, with NaN trend
                                                features imputed to 0.0 (this
                                                mirrors the project's earlier
                                                decision: impute rather than skip
                                                the whole prediction)
  4. gmp_percent available, trend features
     computable                             -> v14_gmp, real trend values

NOTE on the non-GMP / trend-less fallback models (mainboard_bucket_model_v13.pkl,
sme_bucket_model_v7.pkl) and the older v13_gmp/v7_gmp pair: this module expects
them alongside v14_gmp in MODEL_DIR, same convention as the existing predict.py.
They were not re-uploaded in this session, so _load() will return None for any
that aren't present locally -- predict_live_for_company() then raises a clear
PredictionError naming the missing file rather than crashing, same pattern as
predict.py.

STILL OPEN / NOT DECIDED: where a live prediction gets PERSISTED once computed
("stored ready-to-serve", per the original 9-step plan's step 5). There's no
predictions table in the current schema (checked: ipo_master_records,
ipo_live_tracker, gmp_trend, subscription_daywise -- none of them have columns
for storing a bucket-probability prediction). Options: (a) add prediction
columns to ipo_live_tracker, overwritten each poll, or (b) a new
live_predictions table, one row per poll per company (keeps history of how
the prediction evolved, consistent with how Stage 4's trajectory predictions
are meant to be versioned). Not implemented here -- flagged for a decision
before wiring this into the scheduler.
"""

from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd
import pickle
import sqlite3

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

from ipo_model_utils import SectorTargetEncoder  # noqa: F401,E402 -- required for unpickling
from gmp_trend_features import compute_features_for_company  # noqa: E402

MAINBOARD_MODEL_PATH = MODEL_DIR / "mainboard_bucket_model_v13.pkl"
MAINBOARD_GMP_MODEL_PATH = MODEL_DIR / "mainboard_bucket_model_v13_gmp.pkl"
MAINBOARD_V14_GMP_MODEL_PATH = MODEL_DIR / "mainboard_bucket_model_v14_gmp.pkl"
SME_MODEL_PATH = MODEL_DIR / "sme_bucket_model_v7.pkl"
SME_GMP_MODEL_PATH = MODEL_DIR / "sme_bucket_model_v7_gmp.pkl"
SME_V14_GMP_MODEL_PATH = MODEL_DIR / "sme_bucket_model_v14_gmp.pkl"


def _load(path: Path):
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


_mb_model = _load(MAINBOARD_MODEL_PATH)
_mb_gmp_model = _load(MAINBOARD_GMP_MODEL_PATH)
_mb_v14_gmp_model = _load(MAINBOARD_V14_GMP_MODEL_PATH)
_sme_model = _load(SME_MODEL_PATH)
_sme_gmp_model = _load(SME_GMP_MODEL_PATH)
_sme_v14_gmp_model = _load(SME_V14_GMP_MODEL_PATH)


class PredictionError(Exception):
    pass


def _expected_cols(model_pkg) -> set:
    expected = set()
    pre = model_pkg["model"].named_steps.get("pre") or model_pkg["model"].named_steps.get("prep")
    for _, _, cols in pre.transformers:
        if isinstance(cols, str):
            expected.add(cols)
        else:
            expected.update(cols)
    return expected


def _build_feature_row(model_pkg, values: dict) -> pd.DataFrame:
    """values: dict that may contain subscription_total, sector, gmp_percent,
    gmp_pct_trend_slope, gmp_pct_days_since_last_drop, gmp_pct_change_1d,
    gmp_pct_close_to_listing_delta. Only the columns the model's own
    ColumnTransformer actually declares get pulled in -- same dynamic
    detection approach as the existing predict.py, extended for the new
    trend columns."""
    expected = _expected_cols(model_pkg)
    row = {}
    if "subscription_total" in expected:
        row["subscription_total"] = [float(values["subscription_total"])]
    if "log_sub" in expected:
        row["log_sub"] = [float(np.log1p(max(float(values["subscription_total"]), 0)))]
    if "sector" in expected:
        row["sector"] = [values.get("sector") or "__missing__"]
    if "gmp_percent" in expected:
        row["gmp_percent"] = [float(values["gmp_percent"])]
    for trend_col in (
        "gmp_pct_trend_slope",
        "gmp_pct_days_since_last_drop",
        "gmp_pct_change_1d",
        "gmp_pct_close_to_listing_delta",
    ):
        if trend_col in expected:
            v = values.get(trend_col)
            # Imputation decision (made earlier in this project): missing
            # trend features -> 0.0 rather than skipping the prediction.
            row[trend_col] = [0.0 if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)]
    return pd.DataFrame(row)


def _run_model(model_pkg, values: dict) -> dict:
    m = model_pkg["model"]
    X = _build_feature_row(model_pkg, values)
    proba = m.predict_proba(X)[0]
    n_buckets = len(model_pkg["bucket_labels"])
    proba_full = np.zeros(n_buckets)
    for j, c in enumerate(m.classes_):
        proba_full[int(c)] = proba[j]
    top_i = int(np.argmax(proba_full))
    buckets = [
        {"label": lbl, "probability": round(float(proba_full[i]), 4), "most_likely": i == top_i}
        for i, lbl in enumerate(model_pkg["bucket_labels"])
    ]
    return {
        "buckets": buckets,
        "top_bucket": model_pkg["bucket_labels"][top_i],
        "features_used": model_pkg["features"],
    }


def save_prediction(conn: sqlite3.Connection, result: dict) -> None:
    """Persists one live prediction as a new row in live_predictions (never
    overwrites a prior row -- see migration_002 for why: keeps full history
    of how the prediction moved as GMP/subscription numbers changed through
    the day, same versioning approach already decided for Stage 4's
    trajectory predictions)."""
    import json
    from datetime import datetime, timezone

    def _clean(v):
        if v is None:
            return None
        if isinstance(v, float) and np.isnan(v):
            return None
        return v

    values = result["inputs_used"]
    conn.execute(
        """INSERT INTO live_predictions
           (company_name, issue_category, model_version, top_bucket,
            bucket_probabilities, subscription_total, gmp_percent,
            gmp_pct_trend_slope, gmp_pct_days_since_last_drop,
            gmp_pct_change_1d, gmp_pct_close_to_listing_delta, predicted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            result["company_name"],
            result["issue_category"],
            result["model_version"],
            result["prediction"]["top_bucket"],
            json.dumps(result["prediction"]["buckets"]),
            _clean(values.get("subscription_total")),
            _clean(values.get("gmp_percent")),
            _clean(values.get("gmp_pct_trend_slope")),
            _clean(values.get("gmp_pct_days_since_last_drop")),
            _clean(values.get("gmp_pct_change_1d")),
            _clean(values.get("gmp_pct_close_to_listing_delta")),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def predict_live_for_company(conn: sqlite3.Connection, company_name: str, persist: bool = True) -> dict:
    """Main entry point for the live poller / scheduler. Reads
    ipo_live_tracker's current snapshot + gmp_trend history for
    `company_name` and returns a fresh bucket prediction."""
    tracker = pd.read_sql(
        "SELECT * FROM ipo_live_tracker WHERE company_name = ?", conn, params=(company_name,)
    )
    if len(tracker) == 0:
        raise PredictionError(f"'{company_name}' not found in ipo_live_tracker (not currently tracked as open).")
    t = tracker.iloc[0]

    if t["issue_category"] not in ("Mainboard", "SME"):
        raise PredictionError(f"'{company_name}' has no issue_category set in ipo_live_tracker.")

    if t["current_subscription_total"] is None or pd.isna(t["current_subscription_total"]):
        raise PredictionError(f"No current_subscription_total yet for '{company_name}'.")

    values = {
        "subscription_total": t["current_subscription_total"],
        "sector": t["sector"],
        "gmp_percent": None if pd.isna(t["current_gmp_percent"]) else t["current_gmp_percent"],
    }

    gmp_available = values["gmp_percent"] is not None

    if gmp_available:
        hist = pd.read_sql(
            "SELECT * FROM gmp_trend WHERE company_name = ? ORDER BY gmp_date", conn, params=(company_name,)
        )
        trend_feats = compute_features_for_company(hist)
        values.update(trend_feats)

    if t["issue_category"] == "Mainboard":
        model_pkg = _mb_v14_gmp_model if gmp_available and _mb_v14_gmp_model is not None else (
            _mb_gmp_model if gmp_available and _mb_gmp_model is not None else _mb_model
        )
        missing_name = "mainboard_bucket_model_v14_gmp.pkl / v13_gmp.pkl / v13.pkl"
    else:
        model_pkg = _sme_v14_gmp_model if gmp_available and _sme_v14_gmp_model is not None else (
            _sme_gmp_model if gmp_available and _sme_gmp_model is not None else _sme_model
        )
        missing_name = "sme_bucket_model_v14_gmp.pkl / v7_gmp.pkl / v7.pkl"

    if model_pkg is None:
        raise PredictionError(f"No usable model file found ({missing_name}) -- none present in {MODEL_DIR}.")

    result = _run_model(model_pkg, values)

    out = {
        "company_name": company_name,
        "issue_category": t["issue_category"],
        "model_version": model_pkg.get("issue_category", "") + (
            " v14_gmp" if model_pkg is _mb_v14_gmp_model or model_pkg is _sme_v14_gmp_model
            else " (fallback, no trend features)"
        ),
        "inputs_used": values,
        "prediction": result,
        "as_of": t["as_of"],
    }
    if persist:
        save_prediction(conn, out)
    return out


if __name__ == "__main__":
    # Sanity check: ipo_live_tracker is empty in the uploaded DB (poller
    # hasn't run against this copy), so this simulates a "live" row using
    # real ipo_master_records + gmp_trend data for a company that has both,
    # just to prove the wiring works end-to-end. NOT a substitute for
    # testing against a real ipo_live_tracker row once the poller has run.
    conn = sqlite3.connect(str(MODEL_DIR / "ipo_database.db"))
    conn.execute("""CREATE TABLE IF NOT EXISTS live_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL,
        issue_category TEXT, model_version TEXT, top_bucket TEXT,
        bucket_probabilities TEXT, subscription_total REAL, gmp_percent REAL,
        gmp_pct_trend_slope REAL, gmp_pct_days_since_last_drop REAL,
        gmp_pct_change_1d REAL, gmp_pct_close_to_listing_delta REAL,
        predicted_at TEXT NOT NULL)""")

    company = "Advit Jewels"
    rec = pd.read_sql("SELECT * FROM ipo_master_records WHERE company_name = ?", conn, params=(company,)).iloc[0]
    conn.execute(
        """INSERT INTO ipo_live_tracker
           (company_name, issue_category, sector, current_subscription_total, current_gmp_percent, as_of)
           VALUES (?,?,?,?,?,?)""",
        (company, rec["issue_category"], rec["sector"], rec["subscription_total"], rec["gmp_percent"], "TEST"),
    )
    conn.commit()

    out = predict_live_for_company(conn, company)
    import json
    print(json.dumps(out, indent=2, default=str))

    print("\nPersisted row in live_predictions:")
    row = conn.execute(
        "SELECT * FROM live_predictions WHERE company_name = ? ORDER BY id DESC LIMIT 1", (company,)
    ).fetchone()
    print(row)

    conn.execute("DELETE FROM ipo_live_tracker WHERE as_of = 'TEST'")
    conn.execute("DELETE FROM live_predictions WHERE company_name = ?", (company,))
    conn.commit()
