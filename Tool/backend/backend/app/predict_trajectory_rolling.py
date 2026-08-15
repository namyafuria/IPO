"""
predict_trajectory_rolling.py -- serving module for the "rolling"
trajectory models (see project plan §75). These are the models that
recompute a day5/day10 bucket prediction using the ACTUAL observed
early-day price move, once it's known, instead of only pre-listing
features. §74 found this observed-move feature is a large, robust
edge over pre-listing-only features (100% win rate across 100 repeated
CV runs), which is what these models exploit.

Model files expected next to this module (produced by the rolling
training script, not rebuild_problem_b.py -- see project plan §75):
    {mainboard,sme}_day{5,10}_rolling.pkl

Each pkl is a dict with:
    pipeline                    -- fitted sklearn Pipeline (prep + clf)
    bucket_bins                 -- list of 6 quantile edges (5 buckets)
    bucket_labels                -- list of 5 human-readable range strings
    feature_cols                 -- e.g. ['subscription_total', 'day1_to_day2_pct', 'sector']
    feature_day_col              -- e.g. 'price_day2' -- the DB column the
                                     "known_price" argument corresponds to
    feature_name                 -- e.g. 'day1_to_day2_pct' -- the computed
                                     pct-change column name the pipeline expects
    target_day_col                -- e.g. 'price_day5' -- what's being predicted
    category                      -- 'Mainboard' or 'SME'
    n_train_rows
    holdout_accuracy_pct / holdout_naive_accuracy_pct -- validated on a
                                     time-based holdout (last 15% of rows)

Unlike the base Problem B models (bucket_edges are FIXED business-defined
cutoffs: Loss/Flat/Gain/Strong Gain), these rolling models use per-model
QUANTILE bucket edges (bucket_bins), because they were trained on a
different, smaller row subset per horizon -- so bucket_labels is a list
of numeric ranges, not named categories. Callers should not assume the
label set matches the base models'.

This module is intentionally DB-free / side-effect-free: the caller
(predict_trajectory.py's predict_trajectory_smart_for_company) is
responsible for deciding whether a rolling prediction even applies
(price_day1 and the relevant known_price both populated) and for
resolving those values from the DB. This module just does the math.
"""

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pickle

BACKEND_DIR = Path(__file__).resolve().parent.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# SectorTargetEncoder is NOT needed for these pkls (rolling models use
# OneHotEncoder for sector, confirmed against the real uploaded files --
# see project plan), but importing it here anyway would be harmless if
# ever needed. Left out on purpose: this module should load cleanly even
# in a context where ipo_model_utils.py isn't on the path yet.

_ROLLING_SPECS = {
    ("Mainboard", "day5"): "mainboard_day5_rolling.pkl",
    ("Mainboard", "day10"): "mainboard_day10_rolling.pkl",
    ("SME", "day5"): "sme_day5_rolling.pkl",
    ("SME", "day10"): "sme_day10_rolling.pkl",
}


def _load(path: Path):
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


# Loaded once at import time, same lifecycle as predict_trajectory.py's
# base/gmp/recent model dicts.
_MODELS_ROLLING = {
    key: _load(BACKEND_DIR / fname) for key, fname in _ROLLING_SPECS.items()
}


def predict_trajectory_rolling(
    category: str,
    horizon: str,
    subscription_total: float,
    sector: Optional[str],
    price_day1: float,
    known_price: float,
) -> Optional[dict]:
    """Returns a prediction dict shaped like predict_trajectory.py's
    _run_model() output (buckets/top_bucket/model_stats/etc.) so callers
    can drop it into the same `horizons_out[...]` slot without special
    casing, plus a couple of rolling-specific fields
    (observed_move_pct/known_price/feature_day_col/target_day_col) that
    the frontend can use to render "using actual day-N data" copy (see
    project plan §78 item 4, not yet built).

    Returns None if no rolling model exists for this (category, horizon)
    pair, or if price_day1 is falsy (can't compute a pct move from it) --
    callers should treat None as "stay on the pre-listing prediction",
    never as an error.
    """
    model_pkg = _MODELS_ROLLING.get((category, horizon))
    if model_pkg is None:
        return None
    if not price_day1:
        return None

    feature_name = model_pkg["feature_name"]
    observed_move_pct = ((known_price - price_day1) / price_day1) * 100.0

    row = {"subscription_total": [float(subscription_total)]}
    row[feature_name] = [float(observed_move_pct)]
    if "sector" in model_pkg["feature_cols"]:
        row["sector"] = [sector if sector else "__missing__"]
    X = pd.DataFrame(row)

    pipeline = model_pkg["pipeline"]
    proba = pipeline.predict_proba(X)[0]
    n_buckets = len(model_pkg["bucket_labels"])
    proba_full = np.zeros(n_buckets)
    classes = pipeline.named_steps["clf"].classes_
    for j, c in enumerate(classes):
        proba_full[int(c)] = proba[j]

    top_i = int(np.argmax(proba_full))
    buckets = [
        {"label": lbl, "probability": round(float(proba_full[i]), 4), "most_likely": i == top_i}
        for i, lbl in enumerate(model_pkg["bucket_labels"])
    ]

    holdout_acc = model_pkg["holdout_accuracy_pct"]
    naive_acc = model_pkg["holdout_naive_accuracy_pct"]

    return {
        "buckets": buckets,
        "top_bucket": model_pkg["bucket_labels"][top_i],
        "target_definition": (
            f"pct change from {model_pkg['feature_day_col']} (actual, observed) "
            f"to {model_pkg['target_day_col']}, rolling model"
        ),
        "model_variant": "rolling",
        "reliable": float(holdout_acc) > float(naive_acc),
        "reliability_note": (
            f"Holdout accuracy {holdout_acc:.1f}% vs {naive_acc:.1f}% naive baseline, "
            "using the actual observed early-day price move as a feature."
        ),
        "model_stats": {
            "validated_top_bucket_accuracy": round(float(holdout_acc) / 100.0, 4),
            "validated_naive_top_bucket_accuracy": round(float(naive_acc) / 100.0, 4),
            "n_training_rows": model_pkg["n_train_rows"],
        },
        "known_price": known_price,
        "price_day1": price_day1,
        "observed_move_pct": round(observed_move_pct, 2),
        "feature_day_col": model_pkg["feature_day_col"],
        "target_day_col": model_pkg["target_day_col"],
    }
