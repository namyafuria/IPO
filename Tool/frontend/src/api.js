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

export function getTrajectory(name, { subscription } = {}) {
  const params = new URLSearchParams()
  if (subscription != null) params.set('subscription', subscription)
  const qs = params.toString()
  return request(`/api/predict_trajectory/${encodeURIComponent(name)}${qs ? `?${qs}` : ''}`)
}

export { ApiError }
