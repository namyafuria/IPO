import { useEffect, useState, useCallback } from 'react'
import Badge from './Badge'
import LiveHistorySection from './LiveHistorySection'
import PredictedVsActualSection from './PredictedVsActualSection'
import { getListedIpos, getTrajectorySmart, ApiError } from '../api'

const HORIZON_DAYS = [2, 3, 5, 10]

// Step 9. /ipos/listed (routers_live.py) gives the roster of companies
// still inside their Day1-10 window, counted in real NSE trading sessions
// -- not calendar days -- so this list only drops a company once trading
// day 10 has actually happened. Each card then calls the SAME
// /api/predict_trajectory_smart/{name} the search page's TrajectoryPanel
// uses, so the compact prediction shown here is never a second source of
// truth -- just a narrower view of it.
//
// "Compact" per company = whichever horizon is next up given how many
// trading days have elapsed (e.g. elapsed=4 -> show day5, not all four),
// plus that horizon's mode badge (pre_listing vs rolling) so it's clear
// whether the number is a pre-listing estimate or already using an
// actual observed day.
function nextHorizonDay(elapsed) {
  return HORIZON_DAYS.find((d) => d > elapsed) ?? HORIZON_DAYS[HORIZON_DAYS.length - 1]
}

function topBucketOf(horizon) {
  if (!horizon?.buckets?.length) return null
  return horizon.buckets.find((b) => b.most_likely) ?? horizon.buckets[0]
}

function bucketLabel(bucket) {
  if (!bucket) return null
  return bucket.label ?? bucket.bucket ?? bucket.name ?? 'Unknown'
}

function bucketProb(bucket) {
  if (!bucket) return null
  const p = bucket.probability ?? bucket.prob ?? bucket.probability_pct
  if (p == null) return null
  // Normalize to a 0-100 display value regardless of whether the API sent
  // a 0-1 fraction or an already-scaled percentage.
  const pct = p <= 1 ? p * 100 : p
  return `${pct.toFixed(0)}%`
}

function CompanyPrediction({ companyName, elapsed }) {
  const [horizon, setHorizon] = useState(null)
  const [status, setStatus] = useState('loading') // loading | ready | error | no_horizon
  const [errorMessage, setErrorMessage] = useState(null)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setErrorMessage(null)
    getTrajectorySmart(companyName, {})
      .then((data) => {
        if (cancelled) return
        const raw = data.horizons ?? {}
        const targetDay = nextHorizonDay(elapsed)
        const match = Object.entries(raw).find(([key, h]) => {
          const day = h.day ?? Number(key.replace(/\D/g, ''))
          return day === targetDay
        })
        if (match) {
          setHorizon({ ...match[1], day: targetDay })
          setStatus('ready')
        } else {
          setStatus('no_horizon')
        }
      })
      .catch((err) => {
        if (cancelled) return
        // FIX (2026-08-16): main.py's /api/predict_trajectory_smart route
        // maps TrajectoryPredictionError to a 404 with a real, specific
        // reason in `detail` (e.g. "No subscription figure available yet
        // for '<company>'" -- see predict_trajectory.py) -- previously
        // discarded in favor of a generic "Prediction unavailable.",
        // which made every distinct failure look identical and impossible
        // to diagnose from the UI alone.
        setErrorMessage(err instanceof ApiError ? err.message : null)
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [companyName, elapsed])

  if (status === 'loading') {
    return <div className="mt-3 h-8 animate-pulse rounded bg-panel-raised" />
  }
  if (status === 'error') {
    return (
      <p className="mt-3 border-t border-border pt-3 font-mono text-[10px] text-faint">
        {errorMessage || 'Prediction unavailable.'}
      </p>
    )
  }
  if (status === 'no_horizon' || !horizon) {
    return (
      <p className="mt-3 border-t border-border pt-3 font-mono text-[10px] text-faint">
        No prediction available for day {nextHorizonDay(elapsed)} yet.
      </p>
    )
  }

  const top = topBucketOf(horizon)
  return (
    <div className="mt-3 flex items-center justify-between border-t border-border pt-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-muted">Day {horizon.day}</span>
        <Badge tone={horizon.mode === 'rolling' ? 'gain' : 'neutral'}>
          {horizon.mode === 'rolling' ? 'actual data' : 'estimate'}
        </Badge>
      </div>
      <span className="num text-sm font-medium text-amber">
        {bucketLabel(top)}
        {bucketProb(top) ? ` · ${bucketProb(top)}` : ''}
      </span>
    </div>
  )
}

export default function ListedIposPanel() {
  const [ipos, setIpos] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getListedIpos()
      setIpos(data.ipos ?? [])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load listed IPOs.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-2">
        <p className="font-mono text-xs text-muted">
          {ipos.length > 0 ? `${ipos.length} inside Day 1-10 window` : 'Recently listed, tracking Day 1-10'}
        </p>
        <button
          type="button"
          onClick={load}
          className="font-mono text-xs uppercase tracking-wider text-muted transition-colors hover:text-amber"
        >
          Refresh
        </button>
      </div>

      {loading && (
        <div className="flex flex-col gap-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-panel-raised" />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="rounded-md border border-loss/30 bg-loss/5 p-4">
          <p className="text-sm text-ink/90">{error}</p>
        </div>
      )}

      {!loading && !error && ipos.length === 0 && (
        <p className="font-mono text-xs text-faint">
          Nothing currently inside its Day 1-10 window — check Search for anything already past it.
        </p>
      )}

      {!loading && !error && ipos.length > 0 && (
        <div className="flex flex-col gap-3">
          {ipos.map((ipo) => (
            <div
              key={ipo.company_name}
              className="rounded-lg border border-border bg-panel p-4 sm:p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h4 className="font-display text-base font-medium text-ink">
                    {ipo.company_name}
                  </h4>
                  <p className="mt-0.5 font-mono text-[10px] uppercase tracking-wider text-faint">
                    {ipo.issue_category}
                    {ipo.sector ? ` · ${ipo.sector}` : ''} · listed {ipo.listing_date}
                  </p>
                </div>
                <Badge tone="neutral">trading day {ipo.trading_days_elapsed}/10</Badge>
              </div>

              <CompanyPrediction companyName={ipo.company_name} elapsed={ipo.trading_days_elapsed} />
              <PredictedVsActualSection companyName={ipo.company_name} />
              <LiveHistorySection companyName={ipo.company_name} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
