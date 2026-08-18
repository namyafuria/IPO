"""
backfill_trajectory_now.py -- run this ONCE, manually, via Render's Shell
tab (Environment -> Shell in the dashboard, same place you'd run any
one-off command against the live app), to seed a trajectory prediction
for every company currently in the Day1-10 Listed window -- so the whole
Listed tab starts reading cached rows immediately instead of waiting for
each company's next natural bhavcopy touch.

Usage (from the backend root -- the same directory backfill_listed_today.py
already runs from):

    python backfill_trajectory_now.py

Safe to run more than once (insert-only/versioned saves, same as the
routine bhavcopy hook) -- if it fails partway (e.g. Render's shell session
drops), just re-run it.

NOTE: this only imports and calls
scheduler.backfill_all_trajectory_predictions() -- all the actual logic
lives there. Uses an absolute `from app import scheduler` import (not
`from . import scheduler`) so this runs as a plain script -- same
convention as the existing backfill_listed_today.py -- rather than
requiring `python -m app.backfill_trajectory_now`. Adjust the import if
your package is named something other than `app`.
"""

import logging

logging.basicConfig(level=logging.INFO)

from app import scheduler  # adjust if your package isn't named `app`

if __name__ == "__main__":
    result = scheduler.backfill_all_trajectory_predictions()
    print(result)
