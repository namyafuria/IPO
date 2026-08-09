"""
predict_trajectory.py -- Problem B: predicts the post-listing price-
trajectory bucket (day2/day3/day5/day10) for a company, given sector +
subscription_total. Same feature set and lookup pattern as predict.py's
Problem A (predict_for_company) -- this is deliberate, since v1 of
Problem B was trained on exactly those two features (no GMP variant
exists yet, unlike Problem A's v13/v13_gmp split).

Target definition (see rebuild_problem_b.py docstring): the bucket is
%% change from price_day1 (the listing-day close), NOT from issue price.
That means these models are usable identically to Problem A's -- before
OR after actual listing -- since price_day1 is not itself an input
feature, only an output-side reference point. What "Strong Gain" means
here is "expected to keep climbing after its first day", not "expected
to pop on debut" (that's Problem A's job).

Model files expected next to this backend's working directory, produced
by rebuild_problem_b.py: mainboard_bucket_model_day{2,3,5,10}_v1.pkl,
sme_bucket_model_day{2,3,5,10}_v1.pkl.

IMPORTANT CAVEAT carried over from the training session and surfaced in
every response here (see `model_stats` + `reliability_note`): validated
walk-forward accuracy only clearly beats the naive (train-fold class
frequency) baseline for Mainboard day2/day3/day5. Mainboard day10 and
all four SME horizons underperformed naive in validation -- sector +
subscription_total alone do not appear to carry enough signal for
later-horizon trajectory. Callers should treat those four models'
predictions as low-confidence / exploratory, not as reliable signal,
until the feature set is revisited.
"""

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pickle

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Same reasoning as predict.py: pkls were built by rebuild_problem_b.py
# running as a top-level script with a bare `from ipo_model_utils import
# ...`, so pickle stored the class's module path as "ipo_model_utils",
# not "app.ipo_model_utils". Keep ipo_model_utils.py at the backend root
# and put that dir on sys.path so unpickling finds the same module path.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from ipo_model_utils import SectorTargetEncoder  # noqa: F401,E402 -- required for unpickling
from .db import find_company
from .schemas import IPORecord

HORIZONS = [2, 3, 5, 10]
CATEGORIES = ["Mainboard", "SME"]

# Horizons whose validated top-bucket accuracy beat the naive baseline in
# training (see rebuild_problem_b.py's printed results, session of
# 2026-08-09). Everything else still returns a prediction, just flagged.
_RELIABLE = {("Mainboard", 2), ("Mainboard", 3), ("Mainboard", 5)}


def _load(path: Path):
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


# Loaded once at import time, same lifecycle as predict.py's models.
_MODELS = {
    (cat, h): _load(BACKEND_DIR / f"{cat.lower()}_bucket_model_day{h}_v1.pkl")
    for cat in CATEGORIES
    for h in HORIZONS
}


class TrajectoryPredictionError(Exception):
    """Raised for any case where prediction can't proceed -- caller
    (main.py) maps this to an appropriate HTTP response rather than a 500."""


def _build_feature_row(model_pkg, subscription_total, sector) -> pd.DataFrame:
    """Same column-detection logic as predict.py -- read the fitted
    ColumnTransformer's declared columns rather than assuming a fixed
    naming convention."""
    expected_cols = set()
    pre = model_pkg["model"].named_steps.get("pre") or model_pkg["model"].named_steps.get("prep")
    for _, _, cols in pre.transformers:
        if isinstance(cols, str):
            expected_cols.add(cols)
        else:
            expected_cols.update(cols)

    row = {}
    if "subscription_total" in expected_cols:
        row["subscription_total"] = [float(subscription_total)]
    if "sector" in expected_cols:
        row["sector"] = [sector if sector else "__missing__"]
    return pd.DataFrame(row)


def _run_model(model_pkg, subscription_total, sector, category, horizon) -> dict:
    m = model_pkg["model"]
    X = _build_feature_row(model_pkg, subscription_total, sector)

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

    reliable = (category, horizon) in _RELIABLE

    return {
        "buckets": buckets,
        "top_bucket": model_pkg["bucket_labels"][top_i],
        "target_definition": model_pkg["target_definition"],
        "reliable": reliable,
        "reliability_note": (
            "Validated accuracy beat the naive baseline for this category/horizon."
            if reliable else
            "Validated accuracy did NOT clearly beat the naive (train-fold class "
            "frequency) baseline for this category/horizon -- treat this bucket "
            "as low-confidence/exploratory, not a reliable signal."
        ),
        "model_stats": {
            "validated_top_bucket_accuracy": model_pkg["validated_top_bucket_accuracy"],
            "validated_naive_top_bucket_accuracy": model_pkg["validated_naive_top_bucket_accuracy"],
            "validated_log_loss": model_pkg["validated_log_loss"],
            "validated_naive_log_loss": model_pkg["validated_naive_log_loss"],
            "n_training_rows": model_pkg["n_training_rows"],
            "n_rolling_splits": model_pkg["n_rolling_splits"],
        },
    }


def predict_trajectory_for_company(
    name: str,
    subscription_override: Optional[float] = None,
) -> dict:
    """Looks up `name` (DB exact-then-fuzzy, same as /api/company), then
    runs all 4 horizon models for the company's category (Mainboard or
    SME) in one call. Raises TrajectoryPredictionError for every
    "can't predict" case, mirroring predict.py's predict_for_company."""
    record, exact = find_company(name)
    if record is None:
        raise TrajectoryPredictionError(f"No match found for '{name}' in the database.")

    if record.issue_category not in CATEGORIES:
        raise TrajectoryPredictionError(
            f"'{record.company_name}' isn't tagged Mainboard or SME in the database, "
            "so there's no way to know which model to use."
        )

    if subscription_override is not None:
        subscription_total = subscription_override
        is_sub_provisional = True
    elif record.subscription_total is not None:
        subscription_total = record.subscription_total
        is_sub_provisional = False
    else:
        raise TrajectoryPredictionError(
            f"No subscription figure available yet for '{record.company_name}'. "
            "Pass subscription_override with the current live multiple if the issue is still open."
        )

    category = record.issue_category
    horizons_out = {}
    missing = []
    for h in HORIZONS:
        model_pkg = _MODELS.get((category, h))
        if model_pkg is None:
            missing.append(h)
            continue
        horizons_out[f"day{h}"] = _run_model(model_pkg, subscription_total, record.sector, category, h)

    if not horizons_out:
        raise TrajectoryPredictionError(
            f"No Problem B model files found for category '{category}'. "
            "Run rebuild_problem_b.py to generate them."
        )

    return {
        "company_name": record.company_name,
        "exact_match": exact,
        "issue_category": category,
        "inputs_used": {
            "subscription_total": subscription_total,
            "subscription_provisional": is_sub_provisional,
            "sector": record.sector,
        },
        "horizons": horizons_out,
        "horizons_unavailable": missing,
        "actual_outcome": {
            "listing_date": record.listing_date,
            "price_day1": record.price_day1,
            "price_day2": record.price_day2,
            "price_day3": record.price_day3,
            "price_day5": record.price_day5,
            "price_day10": record.price_day10,
        } if record.price_day1 is not None else None,
    }
