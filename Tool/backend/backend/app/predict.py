"""
predict.py — wraps predict_by_name.py's model logic (v13/v13_gmp Mainboard,
v7/v7_gmp SME) for the FastAPI backend. Same lookup, same subscription/gmp
override priority, same automatic GMP-model selection -- just returns a
dict instead of printing, so /api/predict/{name} can serve it as JSON.

Model files expected next to this backend's working directory (same as
predict_by_name.py originally): mainboard_bucket_model_v13(.pkl/_gmp.pkl),
sme_bucket_model_v7(.pkl/_gmp.pkl). SME models are optional -- if missing,
SME companies get a clear "model not available" response rather than a 500.
"""

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pickle

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The pkls were built by rebuild_v13.py running as a top-level script (see
# that file's own `import sys; sys.path.insert(...)` + plain
# `from ipo_model_utils import ...`), so pickle stored the class's module
# path as bare "ipo_model_utils", not "app.ipo_model_utils". Keep
# ipo_model_utils.py at the backend root (not inside app/) and put that dir
# on sys.path so unpickling finds the same module path it was saved with.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from ipo_model_utils import SectorTargetEncoder  # noqa: F401,E402 -- required for unpickling
from .db import find_company
from .schemas import IPORecord

MAINBOARD_MODEL_PATH = BACKEND_DIR / "mainboard_bucket_model_v13.pkl"
MAINBOARD_GMP_MODEL_PATH = BACKEND_DIR / "mainboard_bucket_model_v13_gmp.pkl"
SME_MODEL_PATH = BACKEND_DIR / "sme_bucket_model_v7.pkl"
SME_GMP_MODEL_PATH = BACKEND_DIR / "sme_bucket_model_v7_gmp.pkl"


def _load(path: Path):
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


# Loaded once at import time -- same models served for the process lifetime.
# A future model rebuild needs a process restart to pick up new pkls (fine
# for this project's scale; revisit if that becomes annoying).
_mb_model = _load(MAINBOARD_MODEL_PATH)
_mb_gmp_model = _load(MAINBOARD_GMP_MODEL_PATH)
_sme_model = _load(SME_MODEL_PATH)
_sme_gmp_model = _load(SME_GMP_MODEL_PATH)


class PredictionError(Exception):
    """Raised for any case where prediction can't proceed -- caller (main.py)
    maps this to an appropriate HTTP response rather than a 500."""


def _uses_gmp(model_pkg) -> bool:
    return any("gmp" in f.lower() for f in model_pkg.get("features", []))


def _build_feature_row(model_pkg, subscription_total, sector, gmp_percent) -> pd.DataFrame:
    """Same column-detection logic as predict_by_name.py: read the fitted
    ColumnTransformer's declared columns rather than assuming Mainboard's
    and SME's build scripts used the same feature-naming convention."""
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
    if "log_sub" in expected_cols:
        row["log_sub"] = [float(np.log1p(max(float(subscription_total), 0)))]
    if "sector" in expected_cols:
        row["sector"] = [sector if sector else "__missing__"]
    if "gmp_percent" in expected_cols:
        row["gmp_percent"] = [float(gmp_percent)]
    return pd.DataFrame(row)


def _run_model(model_pkg, subscription_total, sector, gmp_percent) -> dict:
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

    return {
        "buckets": buckets,
        "top_bucket": model_pkg["bucket_labels"][top_i],
        "model_stats": {
            "validated_top_bucket_accuracy": model_pkg["validated_top_bucket_accuracy"],
            "validated_naive_top_bucket_accuracy": model_pkg["validated_naive_top_bucket_accuracy"],
            "validated_log_loss": model_pkg["validated_log_loss"],
            "validated_naive_log_loss": model_pkg["validated_naive_log_loss"],
            "n_training_rows": model_pkg["n_training_rows"],
            "n_rolling_splits": model_pkg["n_rolling_splits"],
        },
    }


def predict_for_company(
    name: str,
    subscription_override: Optional[float] = None,
    gmp_override: Optional[float] = None,
) -> dict:
    """Looks up `name` (DB exact-then-fuzzy, same as /api/company), then runs
    the appropriate bucket model. Raises PredictionError with a clear message
    for every "can't predict" case predict_by_name.py handles on the CLI --
    unknown company, unknown category, no subscription figure, missing model
    file -- so the API layer can turn each into a sensible HTTP response."""
    record, exact = find_company(name)
    if record is None:
        raise PredictionError(f"No match found for '{name}' in the database.")

    if record.issue_category not in ("Mainboard", "SME"):
        raise PredictionError(
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
        raise PredictionError(
            f"No subscription figure available yet for '{record.company_name}'. "
            "Pass subscription_override with the current live multiple if the issue is still open."
        )

    if gmp_override is not None:
        gmp_percent = gmp_override
        is_gmp_provisional = True
    elif record.gmp_percent is not None:
        gmp_percent = record.gmp_percent
        is_gmp_provisional = False
    else:
        gmp_percent = None
        is_gmp_provisional = False

    if record.issue_category == "Mainboard":
        model_pkg = _mb_gmp_model if (gmp_percent is not None and _mb_gmp_model is not None) else _mb_model
        missing_path = MAINBOARD_MODEL_PATH
    else:
        model_pkg = _sme_gmp_model if (gmp_percent is not None and _sme_gmp_model is not None) else _sme_model
        missing_path = SME_MODEL_PATH

    if model_pkg is None:
        raise PredictionError(f"Model file not found: {missing_path.name}")

    result = _run_model(model_pkg, subscription_total, record.sector, gmp_percent)

    return {
        "company_name": record.company_name,
        "exact_match": exact,
        "issue_category": record.issue_category,
        "gmp_aware_model_used": _uses_gmp(model_pkg),
        "inputs_used": {
            "subscription_total": subscription_total,
            "subscription_provisional": is_sub_provisional,
            "sector": record.sector,
            "gmp_percent": gmp_percent,
            "gmp_provisional": is_gmp_provisional,
        },
        "prediction": result,
        "actual_outcome": (
            {"listing_date": record.listing_date, "listing_day_gain_pct": record.listing_day_gain_pct}
            if record.listing_day_gain_pct is not None and record.listing_date is not None
            else None
        ),
    }
