"""
gmp_trend_features.py — computes the 4 day-wise GMP trend features that
mainboard_bucket_model_v14_gmp.pkl and sme_bucket_model_v14_gmp.pkl expect,
from raw gmp_trend rows (company_name, gmp_date, gmp_value, est_profit_pct,
day_tag, ...).

WHY THIS EXISTS: the original feature-engineering script that built
v14_full_df.pkl (used to train these two models) was lost / never saved.
This is a from-scratch reconstruction, NOT a recovery of the original code.
The model files + training script only tell us the feature NAMES, not the
exact formula used to compute them -- so every definition below is a
documented judgment call, not a confirmed match to what the models actually
saw during training. Flagged clearly so the numbers can be sanity-checked
or swapped for the original definitions if those ever turn up.

Used the same way for both:
  - BATCH / historical (rebuilding v14_full_df.pkl equivalent for retraining)
  - LIVE (the hourly poller calling compute_features_for_company() with
    that company's gmp_trend rows so far, to build one live prediction row)

Design choices made explicit here:
  - Dates can have gaps (e.g. no snapshot on a Sunday) -- so "days since"
    and "slope" are computed against real calendar days elapsed between
    gmp_date values, never against row-count.
  - "Latest" = the most recent row by gmp_date for that company, which is
    what a live prediction would use (today's most recent snapshot).
  - Missing/undefined features (e.g. only 1 data point, so no slope is
    possible; or no 'Close' day_tag row exists yet) return NaN. The model
    package's own training script fills NaN -> 0.0 right before predict,
    matching the "impute 0.0" decision already made for this project.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def _to_days(dates: pd.Series) -> pd.Series:
    """gmp_date as real elapsed days (float), for gap-aware slope/diff math."""
    d = pd.to_datetime(dates)
    return (d - d.min()).dt.total_seconds() / 86400.0


def compute_features_for_company(hist: pd.DataFrame, slope_window: int = 5) -> dict:
    """
    hist: gmp_trend rows for ONE company, any order, with at least columns
          gmp_date, est_profit_pct, day_tag.
    slope_window: how many most-recent snapshots to fit the trend slope
          over (default 5 -- arbitrary, documented choice; short enough to
          reflect recent momentum, long enough to not be noise from 2 points).

    Returns dict with the 4 features, each NaN if not computable yet.
    """
    h = hist.dropna(subset=["gmp_date", "est_profit_pct"]).copy()
    h = h.sort_values("gmp_date").reset_index(drop=True)

    out = {
        "gmp_pct_trend_slope": np.nan,
        "gmp_pct_days_since_last_drop": np.nan,
        "gmp_pct_change_1d": np.nan,
        "gmp_pct_close_to_listing_delta": np.nan,
    }
    if len(h) == 0:
        return out

    h["_days"] = _to_days(h["gmp_date"])
    latest_pct = h["est_profit_pct"].iloc[-1]
    latest_day = h["_days"].iloc[-1]

    # --- gmp_pct_trend_slope: slope of est_profit_pct vs calendar days,
    # fit over the last `slope_window` snapshots (least squares). Needs
    # >= 2 points; with exactly 2, this is just the change rate between them.
    tail = h.tail(slope_window)
    if len(tail) >= 2:
        slope, _ = np.polyfit(tail["_days"], tail["est_profit_pct"], 1)
        out["gmp_pct_trend_slope"] = float(slope)

    # --- gmp_pct_days_since_last_drop: calendar days since the most recent
    # day-over-day DECREASE in est_profit_pct (comparing each snapshot to
    # the one before it, not to a fixed baseline). If GMP has never dropped
    # in the available history, this is "days since the first snapshot" as
    # the most defensible fallback (i.e. no drop observed in the whole
    # window we have) rather than 0, which would misleadingly claim a drop
    # just happened.
    diffs = h["est_profit_pct"].diff()
    drop_mask = diffs < 0
    if drop_mask.any():
        last_drop_day = h.loc[drop_mask, "_days"].iloc[-1]
        out["gmp_pct_days_since_last_drop"] = float(latest_day - last_drop_day)
    elif len(h) >= 2:
        out["gmp_pct_days_since_last_drop"] = float(latest_day - h["_days"].iloc[0])

    # --- gmp_pct_change_1d: latest snapshot vs the immediately preceding
    # snapshot (whatever gap that actually is in calendar days -- NOT
    # forced to exactly 24h, since the daily table is one row per day the
    # site published a reading, not guaranteed one row per calendar day).
    if len(h) >= 2:
        out["gmp_pct_change_1d"] = float(h["est_profit_pct"].iloc[-1] - h["est_profit_pct"].iloc[-2])

    # --- gmp_pct_close_to_listing_delta: current/latest est_profit_pct
    # minus est_profit_pct on the row tagged day_tag == 'Close' (the last
    # day of bidding). If the IPO hasn't closed yet (still open, tag not
    # present), this is NaN -- there is no "close" value yet to diff against.
    close_rows = h[h["day_tag"] == "Close"]
    if len(close_rows) > 0:
        close_pct = close_rows["est_profit_pct"].iloc[-1]
        out["gmp_pct_close_to_listing_delta"] = float(latest_pct - close_pct)

    return out


def build_training_features(gmp_trend_df: pd.DataFrame) -> pd.DataFrame:
    """
    BATCH mode: for every (company, date) row in gmp_trend, compute what
    the 4 features would have looked like using only data UP TO AND
    INCLUDING that date (no look-ahead) -- this is what a retraining
    script needs, one feature-row per historical snapshot, not just one
    row per company. Use this to rebuild a v14_full_df.pkl equivalent.

    gmp_trend_df: the full gmp_trend table (all companies).
    Returns a DataFrame with company_name, gmp_date, and the 4 feature cols,
    one row per (company_name, gmp_date) in the input.
    """
    results = []
    for name, g in gmp_trend_df.groupby("company_name"):
        g = g.sort_values("gmp_date").reset_index(drop=True)
        for i in range(len(g)):
            feats = compute_features_for_company(g.iloc[: i + 1])
            feats["company_name"] = name
            feats["gmp_date"] = g["gmp_date"].iloc[i]
            results.append(feats)
    return pd.DataFrame(results)


if __name__ == "__main__":
    # Quick sanity check against real DB data before this gets trusted anywhere.
    import sqlite3

    conn = sqlite3.connect("/mnt/user-data/uploads/ipo_database.db")
    hist = pd.read_sql(
        "SELECT * FROM gmp_trend WHERE company_name = ? ORDER BY gmp_date",
        conn, params=("Advit Jewels",),
    )
    print(hist[["gmp_date", "est_profit_pct", "day_tag"]])
    print()
    print("Features using full history (as if predicting 'live' on the last row):")
    print(compute_features_for_company(hist))
