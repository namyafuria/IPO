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

    category/day     base edge   gmp edge   recent edge (base feats,
                                             gmp-subset population only)
    Mainboard/day2    +0.012     -0.034     +0.018
    Mainboard/day3    +0.005     -0.006     +0.000
    Mainboard/day5    +0.034     +0.079     +0.070
    Mainboard/day10   -0.014     +0.000     +0.018   <- recent_v1 wins, USE
    SME/day2          +0.014     +0.002     n/a
    SME/day3          -0.064     -0.055     n/a  <- both still below naive
    SME/day5          -0.064     -0.048     n/a  <- both still below naive
    SME/day10         -0.031     -0.031     n/a

Mainboard day5 still uses the gmp variant (base +3.4pt, gmp +7.9pt --
genuinely gmp-driven, not just the subset, per the isolation test in
project plan §75). Mainboard day10 switched from gmp_v1 to recent_v1:
the gmp variant only tied naive (+0.000), while training base features
(no gmp_percent as an input) on that SAME row subset actually beat naive
outright (+0.018) -- meaning gmp_percent as a feature was making day10
WORSE, not better, once the row-subset confound is controlled for. Every
other horizon/category still uses the base (sector + subscription_total,
full population) model.

A nifty_trend_pre_listing-augmented variant was also trained and
tested; it did not clearly outperform the GMP-only variant anywhere
and was NOT wired in here to keep the model set simple. Its .pkl files
still exist if this is revisited later. A standalone nifty-only variant
(no gmp) was also tested in project plan §75 and showed a modest,
consistent SME edge (~1-2pt, still below naive on its own) -- not yet
wired in, kept as a reserve option for a future session.

Caveat carried over from the original training session: this
comparison is confounded by the GMP variant being trained on a
different (smaller, ~85%-of-rows) subset than the base model -- some
of the apparent GMP benefit could be that cleaner subset rather than
GMP itself. This was explicitly isolated for Mainboard day5/day10 (see
above); Mainboard day5's edge is genuinely GMP-driven, day10's was not
and has been corrected accordingly. SME's subset effect was similarly
re-checked and found small (~1pt either way) -- not worth the added
complexity there, matching the original call.
"""

import datetime
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
from . import live_fetch
from .db import find_company
from .schemas import IPORecord
from .predict_trajectory_rolling import predict_trajectory_rolling

HORIZONS = [2, 3, 5, 10]
CATEGORIES = ["Mainboard", "SME"]

# Horizons whose validated top-bucket accuracy beat the naive baseline in
# training (see rebuild_problem_b.py's printed results, session of
# 2026-08-09), USING the variant actually selected for that horizon by
# PREFER_GMP/PREFER_RECENT below.
_RELIABLE = {("Mainboard", 2), ("Mainboard", 3), ("Mainboard", 5), ("Mainboard", 10)}

# (category, horizon) pairs where the GMP-augmented model measurably beat
# the base model on walk-forward validation -- see rebuild_problem_b.py's
# docstring table. Every other pair uses the base model even when a gmp
# variant file exists, because gmp measurably hurt or did nothing there.
PREFER_GMP = {("Mainboard", 5)}

# (category, horizon) pairs where the "recent_v1" variant -- base
# features (sector + subscription_total), TRAINING population restricted
# to gmp_percent-available rows, but gmp_percent NOT used as a model
# input -- beat both the plain base model and the gmp variant. Added
# 2026-08-09 after subset-confound testing showed gmp_v1's day10 edge was
# actually this row subset, not gmp_percent as a feature; using
# gmp_percent as a feature for day10 was measurably worse than training
# on the same subset without it. This variant needs NO gmp_percent value
# at inference -- see predict_trajectory_for_company's model selection
# below, which checks this before PREFER_GMP.
PREFER_RECENT = {("Mainboard", 10)}


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
_MODELS_RECENT = {
    (cat, h): _load(BACKEND_DIR / f"{cat.lower()}_bucket_model_day{h}_recent_v1.pkl")
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
        use_recent = (category, h) in PREFER_RECENT and _MODELS_RECENT.get((category, h)) is not None
        use_gmp = (
            not use_recent
            and (category, h) in PREFER_GMP
            and gmp_percent is not None
            and _MODELS_GMP.get((category, h)) is not None
        )
        if use_recent:
            model_pkg = _MODELS_RECENT.get((category, h))
        elif use_gmp:
            model_pkg = _MODELS_GMP.get((category, h))
        else:
            model_pkg = _MODELS_BASE.get((category, h))
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


# ---------------------------------------------------------------------------
# Smart dispatch: per-horizon switch between the pre-listing model above and
# the rolling model (predict_trajectory_rolling.py), rather than a single
# company-level switch. day2/day3 never have a rolling variant (nothing to
# roll from that early). day5 switches to rolling once price_day2 is known;
# day10 switches once price_day5 is known -- independently, so a company can
# have day5 on rolling while day10 is still pre-listing.
# ---------------------------------------------------------------------------
ROLLING_SPECS = [("day5", "price_day2"), ("day10", "price_day5")]


def predict_trajectory_smart_for_company(
    name: str,
    subscription_override: Optional[float] = None,
    gmp_override: Optional[float] = None,
) -> dict:
    """Per-horizon dispatch between pre-listing and rolling models.

    day2/day3 always use the pre-listing model (no rolling variant exists
    for them). day5 switches to rolling once price_day2 is known; day10
    switches once price_day5 is known -- independently.

    If the company has listed but price_day1 is still empty (listed but
    never fetched, vs genuinely upcoming), this fetches synchronously via
    live_fetch.fetch_and_upsert() before deciding -- a stale DB row should
    not be silently read as "still pre-listing".

    Each horizon's result is tagged with a "mode" key ("pre_listing" or
    "rolling") so callers (and the frontend) don't have to infer mode from
    which price fields are null.
    """
    record, exact = find_company(name)
    if record is None:
        raise TrajectoryPredictionError(f"No match found for '{name}' in the database.")

    listed = (
        record.listing_date is not None
        and record.listing_date[:10] <= datetime.date.today().isoformat()
    )
    if listed and record.price_day1 is None:
        try:
            live_fetch.fetch_and_upsert(name)
        except LookupError:
            pass  # record already exists in DB; this shouldn't fire in practice
        record, exact = find_company(name)  # re-read whatever the fetch just wrote

    base = predict_trajectory_for_company(name, subscription_override, gmp_override)
    horizons = base["horizons"]
    for h in horizons:
        horizons[h]["mode"] = "pre_listing"

    for horizon, known_col in ROLLING_SPECS:
        known_price = getattr(record, known_col, None)
        if known_price is None or record.price_day1 is None:
            continue  # stays pre_listing, already tagged above

        rolling_result = predict_trajectory_rolling(
            category=record.issue_category,
            horizon=horizon,
            subscription_total=base["inputs_used"]["subscription_total"],
            sector=record.sector,
            price_day1=record.price_day1,
            known_price=known_price,
        )
        if rolling_result is not None:
            rolling_result["mode"] = "rolling"
            horizons[horizon] = rolling_result

    base["horizons"] = horizons
    return base
