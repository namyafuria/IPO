const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function request(path, { method = 'GET' } = {}) {
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, { method })
  } catch (err) {
    throw new ApiError('Could not reach the IPO Analyser API. Is the backend running?', 0)
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
  return request(`/api/predict/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`)
}

export function getTrajectory(name, { subscription, gmp } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  if (gmp != null) params.set('gmp', gmp)
  const qs = params.toString()
  return request(`/api/predict_trajectory/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`)
}

export function getTrajectorySmart(name, { subscription, gmp } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  if (gmp != null) params.set('gmp', gmp)
  const qs = params.toString()
  return request(`/api/predict_trajectory_smart/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`)
}

// One-shot: refreshes live GMP data server-side, then returns every
// currently-open or recently-listed company with its full live record AND
// its gain/trajectory predictions already computed -- powers the "Live
// IPOs" tab (no per-company follow-up requests needed).
export function syncAndPredictAll() {
  return request('/api/sync_and_predict', { method: 'POST' })
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

export { ApiError }
