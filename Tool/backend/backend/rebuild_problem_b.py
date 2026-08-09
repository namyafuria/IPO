"""
rebuild_problem_b.py — trains the Problem B trajectory bucket models
(day2/day3/day5/day10, one each for Mainboard and SME -- 8 base models,
plus GMP-augmented and GMP+Nifty-augmented variants where enough data
supports them).

Mirrors Problem A's (mainboard_bucket_model_v13.pkl) architecture:
  - features: sector (SectorTargetEncoder, smoothing=10) + log1p(subscription_total)
    -- base variant, unchanged from before
  - optional additional features, each defining its own variant:
      + gmp_percent               -> "_gmp" variant
      + gmp_percent + nifty_trend_pre_listing -> "_gmp_nifty" variant
  - classifier: LogisticRegression(max_iter=1000)
  - validation: walk-forward TimeSeriesSplit over listing_date order, with
    a naive (train-fold class-frequency) baseline for comparison, same
    spirit as v13's "8 rolling splits, 46.5% acc vs 26.5% naive" note.

Why variants instead of always requiring every feature: gmp_percent is
~85% populated and nifty_trend_pre_listing is populated for essentially
every currently-trainable row, but a still-open IPO calling
predict_trajectory before its own GMP is known (or before the DB has
a value for it) still needs *some* model to fall back to. Each variant
is trained ONLY on rows where its required columns are non-null, and
saved under its own filename so predict_trajectory.py can pick the
richest variant available for a given company at inference time and
fall back to a plainer one when a feature is missing -- same pattern
as Problem A's v13 -> v13_gmp split.

Target (per project plan): % change from price_day1 (listing-day
close), NOT issue price -- day1 itself stays Problem A's job. Fixed
bucket edges across every horizon and category:
    Loss (<-5%) / Flat (-5% to 0%) / Gain (0% to 10%) / Strong Gain (10%+)

GNG Electronics Limited is excluded from training (not just calibration)
pending verification of its -80% day2 move -- see project plan notes on
this row. Re-include once verified one way or the other.
"""

import sys
import sqlite3
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, accuracy_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ipo_model_utils import SectorTargetEncoder  # noqa: E402

DB_PATH = Path(__file__).resolve().parent / "ipo_database.db"
OUT_DIR = Path(__file__).resolve().parent

BUCKET_EDGES = [-np.inf, -5, 0, 10, np.inf]
BUCKET_LABELS = ["Loss (<-5%)", "Flat (-5% to 0%)", "Gain (0% to 10%)", "Strong Gain (10%+)"]
N_CLASSES = len(BUCKET_LABELS)
HORIZONS = [2, 3, 5, 10]
CATEGORIES = ["Mainboard", "SME"]
N_SPLITS = {"Mainboard": 8, "SME": 5}
EXCLUDED_COMPANIES = ["GNG Electronics Limited"]  # unverified outlier, see docstring

# Minimum rows required (per category+horizon, after filtering to rows with
# the variant's required columns non-null) before a variant is trained at
# all. Below this, walk-forward splits get too thin to mean anything --
# skip and let predict_trajectory.py fall back to a plainer variant.
MIN_ROWS_FOR_VARIANT = 20

# Each entry: suffix, log1p-transformed numeric cols, standard-scaled
# numeric cols, extra columns required to be non-null for a row to be
# usable in this variant. Order matters: listed richest-last so main()
# reports in a sensible reading order.
FEATURE_SETS = [
    {
        "suffix": "v1",
        "log_cols": ["subscription_total"],
        "scale_cols": [],
        "requires": [],
    },
    {
        "suffix": "gmp_v1",
        "log_cols": ["subscription_total"],
        "scale_cols": ["gmp_percent"],
        "requires": ["gmp_percent"],
    },
    {
        "suffix": "gmp_nifty_v1",
        "log_cols": ["subscription_total"],
        "scale_cols": ["gmp_percent", "nifty_trend_pre_listing"],
        "requires": ["gmp_percent", "nifty_trend_pre_listing"],
    },
]


def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT company_name, issue_category, sector, listing_date, subscription_total,
                  gmp_percent, nifty_trend_pre_listing,
                  price_day1, price_day2, price_day3, price_day5, price_day10
           FROM ipo_master_records
           WHERE price_day1 IS NOT NULL AND price_day2 IS NOT NULL AND price_day3 IS NOT NULL
             AND price_day5 IS NOT NULL AND price_day10 IS NOT NULL
             AND subscription_total IS NOT NULL AND listing_date IS NOT NULL""",
        conn,
    )
    conn.close()

    # One row (Mindspace Business Parks REIT) has subscription_total = 'NA' as a
    # literal text value, not a real NULL -- SQLite's IS NOT NULL doesn't catch
    # that, and it silently poisons the whole column to object dtype once loaded
    # into pandas, which breaks the log1p transformer downstream. Coerce and drop.
    df["subscription_total"] = pd.to_numeric(df["subscription_total"], errors="coerce")
    dropped = df[df["subscription_total"].isna()]
    if len(dropped):
        print(f"Dropping {len(dropped)} row(s) with a non-numeric subscription_total: "
              f"{dropped['company_name'].tolist()}")
    df = df[df["subscription_total"].notna()]

    # Same non-numeric-string defensive coercion for the two new optional
    # feature columns, in case either has stray text values like the
    # subscription_total 'NA' case above.
    for col in ("gmp_percent", "nifty_trend_pre_listing"):
        before_na = df[col].isna().sum()
        df[col] = pd.to_numeric(df[col], errors="coerce")
        newly_na = df[col].isna().sum() - before_na
        if newly_na:
            print(f"Coerced {newly_na} non-numeric '{col}' value(s) to NaN (will exclude those "
                  f"rows from variants that require this column).")

    df = df[~df.company_name.isin(EXCLUDED_COMPANIES)]

    # A couple of rows have no sector at all. predict_trajectory.py already
    # falls back to "__missing__" for a missing sector at inference time --
    # apply the same sentinel here so training sees the same convention,
    # instead of crashing SectorTargetEncoder on a NaN-vs-NaN comparison
    # during fit().
    n_missing_sector = df["sector"].isna().sum()
    if n_missing_sector:
        print(f"Filling {n_missing_sector} row(s) with missing sector using '__missing__' sentinel.")
    df["sector"] = df["sector"].fillna("__missing__")

    df = df.sort_values("listing_date").reset_index(drop=True)
    return df


def make_pipeline(log_cols, scale_cols) -> Pipeline:
    transformers = [("sector", SectorTargetEncoder(smoothing=10, n_classes=N_CLASSES), "sector")]
    if log_cols:
        transformers.append(("log_num", FunctionTransformer(np.log1p), log_cols))
    if scale_cols:
        transformers.append(("scaled_num", StandardScaler(), scale_cols))
    pre = ColumnTransformer(transformers)
    return Pipeline([("pre", pre), ("clf", LogisticRegression(max_iter=1000))])


def bucketize(pct: pd.Series) -> np.ndarray:
    return pd.cut(pct, bins=BUCKET_EDGES, labels=False, right=True).to_numpy()


def naive_baseline_proba(y_train: np.ndarray, n_test: int, n_classes: int) -> np.ndarray:
    """Predicts each class's train-fold frequency for every test row --
    same 'naive' comparison spirit as v13's baseline."""
    freq = np.bincount(y_train, minlength=n_classes) / len(y_train)
    return np.tile(freq, (n_test, 1))


def train_one(df: pd.DataFrame, category: str, horizon: int, feature_set: dict):
    sub = df[df.issue_category == category].copy()
    for col in feature_set["requires"]:
        sub = sub[sub[col].notna()]
    sub = sub.reset_index(drop=True)

    n = len(sub)
    if n < MIN_ROWS_FOR_VARIANT:
        return None

    pct = (sub[f"price_day{horizon}"] - sub["price_day1"]) / sub["price_day1"] * 100
    y = bucketize(pct)
    feature_cols = ["sector", "subscription_total"] + [
        c for c in ("gmp_percent", "nifty_trend_pre_listing") if c in feature_set["requires"]
    ]
    X = sub[feature_cols]
    n_classes = N_CLASSES

    # Cap n_splits at something the fold size can actually support -- a
    # variant with fewer rows (e.g. SME + gmp_percent required) can't
    # always sustain the same split count as the base model.
    n_splits = min(N_SPLITS[category], max(2, n // 15))
    tscv = TimeSeriesSplit(n_splits=n_splits)

    accs, naive_accs, losses, naive_losses = [], [], [], []
    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        if len(np.unique(y_train)) < 2:
            continue  # can't fit/score a fold with only one class present

        pipe = make_pipeline(feature_set["log_cols"], feature_set["scale_cols"])
        pipe.fit(X_train, y_train)
        proba = pipe.predict_proba(X_test)
        proba_full = np.zeros((len(X_test), n_classes))
        for j, c in enumerate(pipe.named_steps["clf"].classes_):
            proba_full[:, int(c)] = proba[:, j]
        # avoid log(0) for classes unseen in this training fold
        proba_full = np.clip(proba_full, 1e-6, 1 - 1e-6)
        proba_full = proba_full / proba_full.sum(axis=1, keepdims=True)

        preds = proba_full.argmax(axis=1)
        accs.append(accuracy_score(y_test, preds))
        losses.append(log_loss(y_test, proba_full, labels=list(range(n_classes))))

        naive_proba = naive_baseline_proba(y_train, len(X_test), n_classes)
        naive_proba = np.clip(naive_proba, 1e-6, 1 - 1e-6)
        naive_proba = naive_proba / naive_proba.sum(axis=1, keepdims=True)
        naive_preds = naive_proba.argmax(axis=1)
        naive_accs.append(accuracy_score(y_test, naive_preds))
        naive_losses.append(log_loss(y_test, naive_proba, labels=list(range(n_classes))))

    if not accs:
        return None

    # Final production model: fit on the full dataset for this
    # category+horizon+feature_set.
    final_pipe = make_pipeline(feature_set["log_cols"], feature_set["scale_cols"])
    final_pipe.fit(X, y)

    feature_desc = ["sector (smoothed target-encoded, smoothing=10)", "log1p(subscription_total)"]
    if "gmp_percent" in feature_set["requires"]:
        feature_desc.append("gmp_percent (standard-scaled)")
    if "nifty_trend_pre_listing" in feature_set["requires"]:
        feature_desc.append("nifty_trend_pre_listing (standard-scaled)")

    return {
        "model": final_pipe,
        "features": feature_desc,
        "required_features": feature_set["requires"],
        "variant": feature_set["suffix"],
        "algorithm": "LogisticRegression",
        "bucket_edges": BUCKET_EDGES,
        "bucket_labels": BUCKET_LABELS,
        "issue_category": category,
        "horizon_day": horizon,
        "target_definition": "pct change from price_day1 (listing-day close), not issue price",
        "validated_top_bucket_accuracy": round(float(np.mean(accs)), 4) if accs else None,
        "validated_naive_top_bucket_accuracy": round(float(np.mean(naive_accs)), 4) if naive_accs else None,
        "validated_log_loss": round(float(np.mean(losses)), 4) if losses else None,
        "validated_naive_log_loss": round(float(np.mean(naive_losses)), 4) if naive_losses else None,
        "n_rolling_splits": len(accs),
        "n_training_rows": n,
        "calibration_method": "none",
        "validation_note": (
            f"Problem B ({feature_set['suffix']}, {category}, day{horizon}) -- retrained this session. "
            f"{len(EXCLUDED_COMPANIES)} row(s) excluded pending verification: {EXCLUDED_COMPANIES}. "
            "Walk-forward TimeSeriesSplit over listing_date order; naive baseline = "
            "train-fold class frequency predicted for every test row."
        ),
    }


def main():
    df = load_data()
    print(f"Loaded {len(df)} rows (Mainboard={sum(df.issue_category=='Mainboard')}, "
          f"SME={sum(df.issue_category=='SME')}) after exclusions.\n")

    results = []
    for category in CATEGORIES:
        for horizon in HORIZONS:
            row_summaries = []
            for feature_set in FEATURE_SETS:
                pkg = train_one(df, category, horizon, feature_set)
                if pkg is None:
                    row_summaries.append(f"  [{feature_set['suffix']:14s}] skipped -- fewer than "
                                          f"{MIN_ROWS_FOR_VARIANT} usable rows for this category/horizon.")
                    continue
                fname = f"{category.lower()}_bucket_model_day{horizon}_{feature_set['suffix']}.pkl"
                with open(OUT_DIR / fname, "wb") as f:
                    pickle.dump(pkg, f)
                results.append((category, horizon, fname, pkg))
                row_summaries.append(
                    f"  [{feature_set['suffix']:14s}] n={pkg['n_training_rows']:4d}  "
                    f"splits={pkg['n_rolling_splits']}  "
                    f"acc={pkg['validated_top_bucket_accuracy']}  (naive={pkg['validated_naive_top_bucket_accuracy']})  "
                    f"log_loss={pkg['validated_log_loss']}  (naive={pkg['validated_naive_log_loss']})  "
                    f"-> {fname}"
                )
            print(f"{category} day{horizon}:")
            for line in row_summaries:
                print(line)
            print()

    return results


if __name__ == "__main__":
    main()
