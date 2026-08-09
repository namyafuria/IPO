# IPO Analyser — Frontend

React + Vite + Tailwind frontend for the IPO Analyser backend (FastAPI).

## Setup

1. **Install dependencies**
   ```
   npm install
   ```
2. **Point the frontend at your backend**
   ```
   cp .env.example .env
   ```
   Then edit `.env` and set `VITE_API_BASE_URL` to wherever the FastAPI backend
   (`ipo-tool-backend-updated.zip` / the live-fetch build from earlier sessions) is running —
   `http://localhost:8000` by default.
3. **Run the backend first.** This frontend has no data of its own — every panel calls the
   FastAPI backend, which owns `ipo_database.db` and the trained models:
   ```
   # in the backend project
   uvicorn app.main:app --reload
   ```
4. **Run the frontend dev server**
   ```
   npm run dev
   ```
   Vite prints a local URL (typically `http://localhost:5173`) — open that in a browser.
5. **Search a company.** Try a name already in the DB (e.g. "Ola Electric", "Swiggy", "NSDL")
   to see the full picture — issue details, subscription/GMP, Problem A's bucket-probability
   prediction, and — once listed — Problem B's day-by-day trajectory alongside actual outcomes.
   A name not in the DB triggers the backend's live-fetch fallback automatically.

## Production build

```
npm run build
```
Outputs a static site to `dist/` — deploy it anywhere that serves static files (Vercel,
Netlify, etc., per the architecture agreed in the project plan). Set `VITE_API_BASE_URL` as a
build-time env var on whichever host you use, pointing at the deployed backend URL.

## What each panel does

- **Search bar** — the page's one entry point. Looks up `GET /api/company/{name}` (exact match,
  fuzzy match, or live-fetch if the company isn't in the DB yet).
- **Company panel** — the raw record, grouped into sections (issue overview, subscription &
  GMP, timeline, post-listing prices, live tracking, identifiers). Shows a "closest match" badge
  on a fuzzy hit and a "fetched live" badge when the data came from a live API call rather than
  the DB. The **Refresh** button re-runs the live fetch for the current company
  (`POST /api/company/{name}/refresh`) and replaces the whole record with the fresh one.
- **Prediction panel (Problem A)** — `GET /api/predict/{name}`. The segmented bucket-probability
  bar is the tool's signature visual: each segment's width is that outcome bucket's probability,
  color-ramped from red (loss) through amber to green (strong gain), with the most likely bucket
  at full opacity. Subscription/GMP override inputs let you re-run the prediction with different
  numbers — handy for a still-open issue where you want to test "what if subscription lands at
  20x" before it's real. If the company has already listed, its actual listing-day gain is shown
  next to the prediction for a direct comparison.
- **Trajectory panel (Problem B)** — `GET /api/predict_trajectory/{name}`. One card per horizon
  (day2/3/5/10), each with its own bucket-probability bar. Horizons whose models don't reliably
  beat a naive guess are marked "low reliability" with an explanation, so a shakier prediction
  never looks as trustworthy as a solid one. Currently that's Mainboard day10 and every SME
  horizon (Mainboard day2/3/5 are the reliable ones) — the badge is driven by the API's own
  `reliable` field per horizon, not hardcoded here, so it stays correct if the models are
  retrained. Where the company has already traded that far, the actual % change from the
  listing-day close is shown alongside the prediction.

## Build status — all 4 steps complete, verified against the real backend
- Step 1: search + company lookup against `GET /api/company/{name}`.
- Step 2: Problem A prediction panel (`GET /api/predict/{name}`) — segmented
  bucket-probability bar, subscription/GMP override inputs with a "reset to actual" control,
  model-accuracy-vs-naive footer, "provisional" badge when an override is in use.
- Step 3: Problem B trajectory panel (`GET /api/predict_trajectory/{name}`) —
  a card per horizon reusing the same `BucketBar`, with a "low reliability" badge + note on
  any horizon whose model doesn't reliably beat the naive baseline.
- Step 4: refresh button (re-fetches live data for the current company), actual-outcome
  comparison (listing-day gain next to the Problem A prediction, per-horizon actual % change
  next to each Problem B card), and a mobile pass (tighter padding, 2-column grids that read
  cleanly down to narrow phone widths, no fixed-width elements).
- **Verification pass (2026-08-09):** ran the actual `ipo-tool-backend-updated` backend
  (`uvicorn app.main:app`) and hit `/api/company`, `/api/predict`, and `/api/predict_trajectory`
  for real against the real DB and pkls. Found and fixed two real mismatches:
  - `/api/predict`'s response nests buckets under `prediction.buckets` and accuracy under
    `prediction.model_stats.validated_top_bucket_accuracy` /
    `validated_naive_top_bucket_accuracy` — not top-level as originally guessed. Fixed in
    `PredictionPanel.jsx`.
  - `/api/predict_trajectory`'s `horizons` is a dict keyed `"day2"`/`"day3"`/`"day5"`/`"day10"`
    (not an array), with the same `model_stats` nesting per horizon. Fixed in
    `TrajectoryPanel.jsx`.
  Both panels now read the real, confirmed shape directly rather than normalizing guesses.

## Known backend gaps found during verification (not a frontend bug)

- **SME Problem A predictions currently fail.** `sme_bucket_model_v7.pkl` and
  `sme_bucket_model_v7_gmp.pkl` aren't present in `ipo-tool-backend-updated.zip` (only the
  Mainboard v13 pair and all 8 Problem B day-horizon pkls are). `GET /api/predict/{name}` for
  an SME company currently returns a 404 ("Model file not found: sme_bucket_model_v7.pkl").
  `PredictionPanel.jsx` already shows that 404's message via `ErrorPanel`-style text, so it
  fails gracefully — but SME Problem A won't actually work until those two pkl files are
  added back to the backend directory. Problem B and the company lookup both work fine for SME.
- **Fuzzy match can be too loose.** Searching `"Ola Electric"` (not the full legal name) matched
  a live-tested backend to an unrelated SME company, "Rulka Electricals Limited", instead of
  "Ola Electric Mobility Limited" — the `difflib.get_close_matches(cutoff=0.6)` logic in
  `app/db.py` scored that as close enough. Not something the frontend can fix (it just
  displays whatever the API returns, with a "closest match" badge when `exact_match` is
  false) — worth knowing when testing, and worth revisiting the cutoff/algorithm in `db.py`
  if it comes up again.

## Setting up the backend (`ipo-tool-backend-updated.zip`)

1. **Unzip and enter the project**
   ```
   unzip ipo-tool-backend-updated.zip
   cd ipo-tool/backend
   ```
2. **Create a virtual environment (recommended) and install dependencies**
   ```
   python3 -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Set up environment variables**
   ```
   cp .env.example .env
   ```
   Edit `.env` and fill in:
   - `IPOGURU_API_KEY` — free key from emailing `ipoguru.in@gmail.com` (name, app/company,
     use case). Only needed for live-fetching pre-listing data on companies not already in
     the DB — everything already in `ipo_database.db` works without it.
   - `INDIANAPI_API_KEY` — free key from your dashboard at indianapi.in. Needed for
     post-listing live-fetch fields (sector, financials, prices) on new companies.
   - `SYNC_INTERVAL_MINUTES` (default 60) and `POST_LISTING_TRACK_DAYS` (default 12) — safe
     to leave at their defaults.
   - `RUN_SCHEDULER` — leave at `0` unless you're running this on a long-lived host (a VM or
     a Render/Railway web service); set to `1` there to auto-refresh open/recent IPOs in the
     background. On a serverless host (Vercel), leave it `0` and instead point an external
     cron (Vercel Cron, GitHub Actions, etc.) at `POST /api/sync`.
4. **Confirm the database and model files are present.** The zip should already include
   `ipo_database.db` at the backend root, plus these `.pkl` files:
   ```
   mainboard_bucket_model_v13.pkl
   mainboard_bucket_model_v13_gmp.pkl
   mainboard_bucket_model_day2_v1.pkl
   mainboard_bucket_model_day3_v1.pkl
   mainboard_bucket_model_day5_v1.pkl
   mainboard_bucket_model_day10_v1.pkl
   sme_bucket_model_day2_v1.pkl
   sme_bucket_model_day3_v1.pkl
   sme_bucket_model_day5_v1.pkl
   sme_bucket_model_day10_v1.pkl
   ipo_model_utils.py   # required for unpickling — must stay at the backend root, not in app/
   ```
   Missing from the current zip: `sme_bucket_model_v7.pkl` / `sme_bucket_model_v7_gmp.pkl`
   (SME Problem A) — see "Known backend gaps" above. Copy those in from wherever they were
   originally built if you have them, or SME Problem A predictions will 404 until then.
5. **Run the server**
   ```
   uvicorn app.main:app --reload
   ```
   By default this serves on `http://127.0.0.1:8000`. Visit
   `http://127.0.0.1:8000/api/health` in a browser — you should see
   `{"status": "ok" or "degraded", "missing_env_vars": [...]}`. "degraded" just means an API
   key isn't set yet; the DB-backed endpoints (which cover everything already in
   `ipo_database.db`) work regardless.
6. **Then start the frontend** (separate terminal, separate project) as described above —
   point its `VITE_API_BASE_URL` at this backend's URL.

## Note on the /api/predict and /api/predict_trajectory response shapes

Confirmed directly against `ipo-tool-backend-updated.zip`'s real source and a live-running
instance (2026-08-09) — see "Verification pass" above. If the backend's response shape changes
in a future rebuild, the two extraction points to check are `extract()` in
`PredictionPanel.jsx` and `normalizeHorizons()` in `TrajectoryPanel.jsx`.
