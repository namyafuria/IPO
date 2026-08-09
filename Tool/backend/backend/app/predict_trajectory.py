"""
predict_trajectory.py -- Problem B: predicts the post-listing price-
trajectory bucket (day2/day3/day5/day10) for a company, given sector +
subscription_total (+ gmp_percent for the two horizons where it
demonstrably helps -- see PREFER_GMP below).

Target definition (see rebuild_problem_b.py docstring): the bucket is
%% change from price_day1 (the listing-day close), NOT from issue price.
That means these models are usable identically to Problem A's -- before
OR after actual listing -- since price_day1 is not itself an input
feature, only an output-side reference point. What "Strong Gain" means
here is "expected to keep climbing after its first day", not "expected
to pop on debut" (that's Problem A's job).

Model files expected next to this backend's working directory, produced
by rebuild_problem_b.py: {mainboard,sme}_bucket_model_day{2,3,5,10}_v1.pkl
(base, sector + subscription_total only), and where trained,
{mainboard,sme}_bucket_model_day{2,3,5,10}_gmp_v1.pkl (adds gmp_percent).

IMPORTANT -- GMP variant is NOT always better, and is NOT always used.
Retrained + re-validated this session (2026-08-09) comparing each
variant's walk-forward accuracy edge over its own naive baseline:

    category/day     base edge   gmp edge
    Mainboard/day2    +0.012     -0.034   <- gmp WORSE, do not use
    Mainboard/day3    +0.005     -0.006   <- gmp WORSE, do not use
    Mainboard/day5    +0.034     +0.079   <- gmp clearly better, USE
    Mainboard/day10   -0.014     +0.000   <- gmp better (ties naive vs
                                              losing to it), USE
    SME/day2          +0.014     +0.002   <- gmp WORSE, do not use
    SME/day3          -0.064     -0.055   <- gmp slightly less bad, but
                                              both still well below naive
                                              -- not worth the added
                                              complexity, do not use
    SME/day5          -0.064     -0.048   <- same as above, do not use
    SME/day10         -0.031     -0.031   <- no difference, do not use

Only Mainboard day5 and Mainboard day10 use the GMP variant. Every
other horizon/category still uses the base (sector + subscription_total
only) model -- adding GMP there measurably hurt or did nothing.
A nifty_trend_pre_listing-augmented variant was also trained and
tested; it did not clearly outperform the GMP-only variant anywhere
and was NOT wired in here to keep the model set simple. Its .pkl files
still exist if this is revisited later.

Caveat carried over from the original training session: this
comparison is confounded by the GMP variant being trained on a
different (smaller, ~85%-of-rows) subset than the base model -- some
of the apparent GMP benefit could be that cleaner subset rather than
GMP itself. Treat this as a real, reproduced improvement worth using,
not a fully isolated causal claim.
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
# 2026-08-09), USING the variant actually selected for that horizon by
# PREFER_GMP below. Mainboard day10 is deliberately NOT included here even
# though its gmp variant is used (PREFER_GMP) -- gmp only brought it up to
# an exact TIE with naive (edge +0.000), not a genuine win, so it's still
# flagged low-confidence to the caller despite using the better model.
_RELIABLE = {("Mainboard", 2), ("Mainboard", 3), ("Mainboard", 5)}

# (category, horizon) pairs where the GMP-augmented model measurably beat
# the base model on walk-forward validation -- see docstring table above.
# Every other pair uses the base model even when a gmp variant file
# exists, because gmp measurably hurt or did nothing there.
PREFER_GMP = {("Mainboard", 5), ("Mainboard", 10)}


def _load(path: Path):
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


# Loaded once at import time, same lifecycle as predict.py's models.
# Both variants are loaded for every (category, horizon) where the file
# exists; which one gets used per-request is decided in
# predict_trajectory_for_company, not here, so a request without a GMP
# value can still fall back to base even for a PREFER_GMP pair.
_MODELS_BASE = {
    (cat, h): _load(BACKEND_DIR / f"{cat.lower()}_bucket_model_day{h}_v1.pkl")
    for cat in CATEGORIES
    for h in HORIZONS
}
_MODELS_GMP = {
    (cat, h): _load(BACKEND_DIR / f"{cat.lower()}_bucket_model_day{h}_gmp_v1.pkl")
    for cat in CATEGORIES
    for h in HORIZONS
}


class TrajectoryPredictionError(Exception):
    """Raised for any case where prediction can't proceed -- caller
    (main.py) maps this to an appropriate HTTP response rather than a 500."""


def _build_feature_row(model_pkg, subscription_total, sector, gmp_percent) -> pd.DataFrame:
    """Same column-detection logic as predict.py -- read the fitted
    ColumnTransformer's declared columns rather than assuming a fixed
    naming convention. Generic over whichever columns THIS model_pkg's
    pipeline actually expects, so it works unmodified for base, gmp, or
    any future variant."""
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
    if "gmp_percent" in expected_cols:
        if gmp_percent is None:
            raise TrajectoryPredictionError(
                "Internal error: selected a GMP-variant model but no gmp_percent value was resolved."
            )
        row["gmp_percent"] = [float(gmp_percent)]
    return pd.DataFrame(row)


def _run_model(model_pkg, subscription_total, sector, gmp_percent, category, horizon) -> dict:
    m = model_pkg["model"]
    X = _build_feature_row(model_pkg, subscription_total, sector, gmp_percent)

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
        "model_variant": model_pkg["variant"],
        "reliable": reliable,
        "reliability_note": (
            "Validated accuracy beat the naive baseline for this category/horizon "
            f"(using the {model_pkg['variant']} model)."
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
    gmp_override: Optional[float] = None,
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

    # gmp_percent is optional -- only resolved/used at all for the two
    # (category, horizon) pairs in PREFER_GMP. No error if it's missing;
    # those pairs just fall back to the base model, same as any other.
    if gmp_override is not None:
        gmp_percent = gmp_override
        is_gmp_provisional = True
    elif record.gmp_percent is not None:
        gmp_percent = record.gmp_percent
        is_gmp_provisional = False
    else:
        gmp_percent = None
        is_gmp_provisional = False

    category = record.issue_category
    horizons_out = {}
    missing = []
    for h in HORIZONS:
        use_gmp = (category, h) in PREFER_GMP and gmp_percent is not None and _MODELS_GMP.get((category, h)) is not None
        model_pkg = _MODELS_GMP.get((category, h)) if use_gmp else _MODELS_BASE.get((category, h))
        if model_pkg is None:
            missing.append(h)
            continue
        horizons_out[f"day{h}"] = _run_model(
            model_pkg, subscription_total, record.sector,
            gmp_percent if use_gmp else None, category, h,
        )

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
            "gmp_percent": gmp_percent,
            "gmp_provisional": is_gmp_provisional,
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
