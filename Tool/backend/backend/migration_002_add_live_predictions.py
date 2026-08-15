"""
migration_002_add_live_predictions.py — adds a live_predictions table so
each live prediction (one per poll, per company) is kept as its own row
rather than overwritten. Idempotent: safe to run multiple times, only
creates the table if it doesn't already exist.

Schema rationale:
  - id, autoincrement: natural ordering of predictions over time per company.
  - company_name + predicted_at: which company, when this prediction was made.
  - model_version: e.g. "Mainboard v14_gmp" or "SME (fallback, no trend
    features)" -- so later analysis can tell which model generation produced
    which prediction (important once v15+ exists, or if a fallback model was
    used because trend data wasn't available yet that poll).
  - top_bucket + bucket_probabilities (JSON text): the actual prediction.
  - the raw inputs used (subscription_total, gmp_percent, the 4 trend
    features) are stored alongside the prediction itself, not just looked up
    later from gmp_trend/ipo_live_tracker -- those tables get overwritten/
    updated on later polls, so without storing inputs alongside the
    prediction you could never reconstruct "what did the model see when it
    made THIS prediction".
"""

import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "ipo_database.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    issue_category TEXT,
    model_version TEXT,
    top_bucket TEXT,
    bucket_probabilities TEXT,   -- JSON: [{"label":..., "probability":..., "most_likely":...}, ...]
    subscription_total REAL,
    gmp_percent REAL,
    gmp_pct_trend_slope REAL,
    gmp_pct_days_since_last_drop REAL,
    gmp_pct_change_1d REAL,
    gmp_pct_close_to_listing_delta REAL,
    predicted_at TEXT NOT NULL   -- ISO timestamp of this poll's prediction
);
"""

INDEX = """
CREATE INDEX IF NOT EXISTS idx_live_predictions_company
ON live_predictions(company_name, predicted_at);
"""

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.execute(INDEX)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM live_predictions").fetchone()[0]
    print(f"live_predictions table ready in {DB_PATH} ({n} existing rows).")
    conn.close()
