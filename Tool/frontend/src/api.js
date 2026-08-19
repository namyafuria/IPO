const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function request(path, { method = 'GET', timeoutMs = 30000 } = {}) {
  let res
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    res = await fetch(`${API_BASE}${path}`, { method, signal: controller.signal })
  } catch (err) {
    if (err.name === 'AbortError') {
      // Render's free tier spins the backend down when idle -- a cold
      // start can take 30-50s. Failing fast here (instead of letting the
      // browser hang indefinitely) means the UI can show a clear "still
      // waking up, try again" message and re-enable its refresh/retry
      // button, rather than leaving a spinner stuck forever.
      throw new ApiError('The API is taking a while to respond (it may be waking up from idle). Please try again in a few seconds.', 0)
    }
    throw new ApiError('Could not reach the IPO Analyser API. Is the backend running?', 0)
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      /* ignore parse failure, fall back to statusText */
    }
    throw new ApiError(detail, res.status)
  }
  return res.json()
}

export function getCompany(name) {
  return request(`/api/company/${encodeURIComponent(name)}`)
}

export function refreshCompany(name) {
  return request(`/api/company/${encodeURIComponent(name)}/refresh`, { method: 'POST' })
}

export function getPrediction(name, { subscription, gmp } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  if (gmp != null) params.set('gmp', gmp)
  const qs = params.toString()
  // FIX (2026-08-17): was on the default 20s budget. Confirmed failing
  // in production for open/awaiting-listing companies (e.g. Dhoot
  // Transmission's "Listing-day gain prediction" panel) with the
  // "waking up from idle" message even though the backend was already
  // warm -- same root cause getTrajectorySmart was fixed for on 2026-08-16
  // (predict can trigger a synchronous live-data fetch server-side before
  // responding). Bumped 45s -> 60s (2026-08-17): Render's own dashboard
  // warns cold starts can take "50 seconds or more", so 45s didn't leave
  // enough margin -- still saw the "waking up from idle" message on
  // responses that were just legitimately slow, not actually failing.
  return request(`/api/predict/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`, { timeoutMs: 60000 })
}

export function getTrajectory(name, { subscription, gmp } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  if (gmp != null) params.set('gmp', gmp)
  const qs = params.toString()
  // FIX (2026-08-17): same call class as getPrediction/getTrajectorySmart
  // above -- bumped preventively for consistency, not yet confirmed
  // failing in production like the other two were.
  return request(`/api/predict_trajectory/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`, { timeoutMs: 60000 })
}

export function getTrajectorySmart(name, { subscription, gmp } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  if (gmp != null) params.set('gmp', gmp)
  const qs = params.toString()
  // FIX (2026-08-16): predict_trajectory_smart_for_company() can trigger a
  // synchronous live_fetch.fetch_and_upsert() call (an external Indian API
  // network round-trip) inline, before responding, for any listed company
  // still missing price_day1 -- unlike /ipos/open, which only ever reads
  // already-cached data. The Listed tab fires one of these per card,
  // concurrently, against what may be a single-worker free-tier instance --
  // the default 20s budget was timing out real (if slow) responses and
  // showing a misleading "waking up from idle" message. Bumped 45s -> 60s
  // (2026-08-17) to match the same margin fix applied to getPrediction,
  // since Render's cold starts can run "50 seconds or more" per their own
  // dashboard warning.
  return request(`/api/predict_trajectory_smart/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`, { timeoutMs: 60000 })
}

// One-shot: refreshes live GMP data server-side, then returns every
// currently-open or recently-listed company with its full live record AND
// its gain/trajectory predictions already computed -- powers the "Live
// IPOs" tab (no per-company follow-up requests needed).
export function syncAndPredictAll() {
  return request('/api/sync_and_predict', { method: 'POST', timeoutMs: 60000 })
}

// Step 8: currently-open-for-bidding IPOs, each with its latest live
// (hourly-polled) snapshot + Day-1 prediction already attached server-side.
// No params -- the backend's own hourly poller keeps this fresh, so the
// frontend just reads whatever's current and offers a manual re-fetch.
export function getOpenIpos() {
  return request('/ipos/open')
}

// Step 9: companies still inside their Day1-10 trajectory window (real NSE
// trading days, not calendar days -- see routers_live.py). Returns the
// roster only; each card fetches its own compact prediction via
// getTrajectorySmart, same source of truth as the search page.
export function getListedIpos() {
  return request('/ipos/listed')
}

// Full GMP/subscription/prediction history for one company (routers_live.py
// GET /ipos/{name}/live-history). Keyed by company_name, not a slug --
// there's no slug column anywhere in the DB. Powers the "History" expand
// on Open/Listed cards.
export function getLiveHistory(name) {
  return request(`/ipos/${encodeURIComponent(name)}/live-history`)
}

// Item 3: predicted-vs-actual comparison for one company's saved trajectory
// prediction (routers_predicted_vs_actual.py GET
// /ipos/{slug}/predicted-vs-actual). Keyed loosely -- backend does fuzzy
// find_company() matching, so the plain company name works as the slug,
// same as getLiveHistory().
export function getPredictedVsActual(name) {
  return request(`/ipos/${encodeURIComponent(name)}/predicted-vs-actual`)
}

export { ApiError }
