"""
Rolling / updating trajectory prediction -- serving layer.

Adjusted to match this backend's real conventions (seen in the actual
predict_trajectory.py, not guessed): pkl files live flat at BACKEND_DIR
(the app package's parent folder), same place as the other Problem B
models -- NOT in a "./models" subfolder. Company records are IPORecord
objects (attribute access: record.subscription_total), not dicts.

Drop this file inside the `app` package, next to predict_trajectory.py.
Model files (mainboard_day5_rolling.pkl, mainboard_day10_rolling.pkl,
sme_day5_rolling.pkl, sme_day10_rolling.pkl) go at BACKEND_DIR -- i.e.
the same folder where {category}_bucket_model_day{N}_v1.pkl already live.

Each pkl stores a dict:
{
    'pipeline': fitted sklearn Pipeline (CalibratedClassifierCV over LogisticRegression),
    'bucket_bins': list of bucket edge values (drift_pct),
    'bucket_labels': human-readable label per bucket, e.g. "+2.1% to +5.4%",
    'feature_cols': ['subscription_total', <feature_name>, 'sector'],
    'feature_day_col': the DB column the "actual move so far" is computed from
                        (price_day2 for the day5 model, price_day5 for the day10 model),
    'feature_name': name of the derived feature, e.g. 'day1_to_day2_pct',
    'target_day_col': price_day5 or price_day10,
    'category': 'Mainboard' | 'SME',
    'n_train_rows': int,
    'holdout_accuracy_pct': float,     # honest time-based holdout, NOT training accuracy
    'holdout_naive_accuracy_pct': float,
}

Usage pattern in the API (see main.py's new route):
  - GET /api/predict_trajectory/{name} (existing, unchanged): pre-listing-only
    prediction for all horizons, works before listing_date.
  - NEW: GET /api/predict_trajectory_rolling/{name}?horizon=day5|day10
    Requires the company to already have the earlier day's actual price in the
    DB (price_day2 for day5, price_day5 for day10). main.py returns 409 if
    that price isn't available yet -- this endpoint is only usable once some
    real trading days have elapsed post-listing, by design.
"""
import pickle
import sys
from pathlib import Path

import pandas as pd

# Same convention as predict_trajectory.py: this file lives inside the app
# package, pkls live one level up (the backend root), flat -- not in a
# "models" subfolder.
BACKEND_DIR = Path(__file__).resolve().parent.parent

_MODEL_CACHE = {}


def _load_model(category: str, horizon: str):
    key = (category, horizon)
    if key not in _MODEL_CACHE:
        fname = f"{category.lower()}_{horizon}_rolling.pkl"
        path = BACKEND_DIR / fname
        try:
            with open(path, "rb") as f:
                _MODEL_CACHE[key] = pickle.load(f)
        except FileNotFoundError:
            _MODEL_CACHE[key] = None
    return _MODEL_CACHE[key]


def predict_trajectory_rolling(
    category: str,
    horizon: str,           # "day5" or "day10"
    subscription_total: float,
    sector: str,
    price_day1: float,
    known_price: float,     # actual price_day2 (for day5 horizon) or price_day5 (for day10 horizon)
):
    """
    Returns None if the model isn't available for this category/horizon, or
    if price_day1/known_price aren't usable yet. Otherwise returns a dict
    matching the shape of the existing /api/predict_trajectory response for
    one horizon, plus a 'basis' field explaining what actual data was used.
    """
    if horizon not in ("day5", "day10"):
        raise ValueError(
            "horizon must be 'day5' or 'day10' - rolling models only exist "
            "for these two (day2/day3 are close enough to listing that the "
            "pre-listing-only model is already the right tool; day1 has no "
            "earlier day to roll from at all)"
        )

    model = _load_model(category, horizon)
    if model is None:
        return None

    if price_day1 is None or price_day1 <= 0 or known_price is None:
        return None

    actual_move_pct = (known_price - price_day1) / price_day1 * 100

    X = pd.DataFrame([{
        "subscription_total": subscription_total,
        model["feature_name"]: actual_move_pct,
        "sector": sector or "unknown",
    }])[model["feature_cols"]]

    pipe = model["pipeline"]
    probs = pipe.predict_proba(X)[0]
    pred_bucket = int(probs.argmax())

    return {
        "horizon": horizon,
        "mode": "rolling",
        "basis": f"uses actual {model['feature_day_col']} (observed {actual_move_pct:+.1f}% "
                 f"move from listing day) in addition to subscription+sector",
        "predicted_bucket": pred_bucket,
        "predicted_bucket_label": model["bucket_labels"][pred_bucket],
        "bucket_probabilities": [
            {"label": lbl, "probability": round(float(p), 4)}
            for lbl, p in zip(model["bucket_labels"], probs)
        ],
        "model_stats": {
            "validated_holdout_accuracy_pct": model["holdout_accuracy_pct"],
            "validated_naive_accuracy_pct": model["holdout_naive_accuracy_pct"],
            "n_train_rows": model["n_train_rows"],
        },
        "reliable": bool((model["holdout_accuracy_pct"] - model["holdout_naive_accuracy_pct"]) > 5),
    }
