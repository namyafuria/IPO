"""
rebuild_problem_b.py — trains the Problem B trajectory bucket models
(day2/day3/day5/day10, one each for Mainboard and SME -- 8 models total).

Mirrors Problem A's (mainboard_bucket_model_v13.pkl) architecture exactly,
so predict.py's existing model-loading/feature-detection logic works
unmodified for these too:
  - features: sector (SectorTargetEncoder, smoothing=10) + log1p(subscription_total)
  - classifier: LogisticRegression(max_iter=1000)
  - validation: walk-forward TimeSeriesSplit over listing_date order, with
    a naive (train-fold class-frequency) baseline for comparison, same
    spirit as v13's "8 rolling splits, 46.5% acc vs 26.5% naive" note.

Target (per project plan, finalized this session): % change from
price_day1 (listing-day close), NOT issue price -- day1 itself stays
Problem A's job. Fixed bucket edges across every horizon and category
(deliberately simple, per user decision over the per-horizon/per-category
alternative):
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
from sklearn.preprocessing import FunctionTransformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ipo_model_utils import SectorTargetEncoder  # noqa: E402

DB_PATH = Path(__file__).resolve().parent / "ipo_database.db"
OUT_DIR = Path(__file__).resolve().parent

BUCKET_EDGES = [-np.inf, -5, 0, 10, np.inf]
BUCKET_LABELS = ["Loss (<-5%)", "Flat (-5% to 0%)", "Gain (0% to 10%)", "Strong Gain (10%+)"]
HORIZONS = [2, 3, 5, 10]
CATEGORIES = ["Mainboard", "SME"]
N_SPLITS = {"Mainboard": 8, "SME": 5}
EXCLUDED_COMPANIES = ["GNG Electronics Limited"]  # unverified outlier, see docstring


def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT company_name, issue_category, sector, listing_date, subscription_total,
                  gmp_percent, price_day1, price_day2, price_day3, price_day5, price_day10
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
    before = len(df)
    df["subscription_total"] = pd.to_numeric(df["subscription_total"], errors="coerce")
    dropped = df[df["subscription_total"].isna()]
    if len(dropped):
        print(f"Dropping {len(dropped)} row(s) with a non-numeric subscription_total: "
              f"{dropped['company_name'].tolist()}")
    df = df[df["subscription_total"].notna()]

    df = df[~df.company_name.isin(EXCLUDED_COMPANIES)]

    # A couple of rows have no sector at all. predict.py already falls back to
    # "__missing__" for a missing sector at inference time (see _build_feature_row) --
    # apply the same sentinel here so training sees the same convention, instead of
    # crashing SectorTargetEncoder on a NaN-vs-NaN comparison during fit().
    n_missing_sector = df["sector"].isna().sum()
    if n_missing_sector:
        print(f"Filling {n_missing_sector} row(s) with missing sector using '__missing__' sentinel.")
    df["sector"] = df["sector"].fillna("__missing__")

    df = df.sort_values("listing_date").reset_index(drop=True)
    return df


def make_pipeline() -> Pipeline:
    pre = ColumnTransformer([
        ("sector", SectorTargetEncoder(smoothing=10), "sector"),
        ("sub", FunctionTransformer(np.log1p), ["subscription_total"]),
    ])
    return Pipeline([("pre", pre), ("clf", LogisticRegression(max_iter=1000))])


def bucketize(pct: pd.Series) -> np.ndarray:
    return pd.cut(pct, bins=BUCKET_EDGES, labels=False, right=True).to_numpy()


def naive_baseline_proba(y_train: np.ndarray, n_test: int, n_classes: int) -> np.ndarray:
    """Predicts each class's train-fold frequency for every test row --
    same 'naive' comparison spirit as v13's baseline."""
    freq = np.bincount(y_train, minlength=n_classes) / len(y_train)
    return np.tile(freq, (n_test, 1))


def train_one(df: pd.DataFrame, category: str, horizon: int) -> dict:
    sub = df[df.issue_category == category].reset_index(drop=True)
    pct = (sub[f"price_day{horizon}"] - sub["price_day1"]) / sub["price_day1"] * 100
    y = bucketize(pct)
    X = sub[["sector", "subscription_total"]]
    n = len(sub)
    n_classes = len(BUCKET_LABELS)

    n_splits = N_SPLITS[category]
    tscv = TimeSeriesSplit(n_splits=n_splits)

    accs, naive_accs, losses, naive_losses = [], [], [], []
    for train_idx, test_idx in tscv.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        if len(np.unique(y_train)) < 2:
            continue  # can't fit/score a fold with only one class present

        pipe = make_pipeline()
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

    # Final production model: fit on the full dataset for this category+horizon.
    final_pipe = make_pipeline()
    final_pipe.fit(X, y)

    return {
        "model": final_pipe,
        "features": ["sector (smoothed target-encoded, smoothing=10)", "log1p(subscription_total)"],
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
            f"Problem B v1 ({category}, day{horizon}) -- trained this session. "
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
            pkg = train_one(df, category, horizon)
            fname = f"{category.lower()}_bucket_model_day{horizon}_v1.pkl"
            with open(OUT_DIR / fname, "wb") as f:
                pickle.dump(pkg, f)
            results.append((category, horizon, fname, pkg))
            print(f"{category:9s} day{horizon:<3d} n={pkg['n_training_rows']:4d}  "
                  f"splits={pkg['n_rolling_splits']}  "
                  f"acc={pkg['validated_top_bucket_accuracy']}  (naive={pkg['validated_naive_top_bucket_accuracy']})  "
                  f"log_loss={pkg['validated_log_loss']}  (naive={pkg['validated_naive_log_loss']})  "
                  f"-> {fname}")

    return results


if __name__ == "__main__":
    main()
