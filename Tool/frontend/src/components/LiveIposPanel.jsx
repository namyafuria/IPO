import { useState } from 'react'
import LiveIpoCard from './LiveIpoCard'
import ErrorPanel from './ErrorPanel'
import { syncAndPredictAll, ApiError } from '../api'

export default function LiveIposPanel() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [predictions, setPredictions] = useState(null)
  const [hasRun, setHasRun] = useState(false)

  async function handleClick() {
    setLoading(true)
    setError(null)
    try {
      const data = await syncAndPredictAll()
      setPredictions(data.predictions ?? [])
      setHasRun(true)
    } catch (err) {
      setPredictions(null)
      setError(err instanceof ApiError ? err.message : 'Something went wrong.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={handleClick}
          disabled={loading}
          className="rounded-md border border-amber-dim bg-panel-raised px-4 py-2.5 font-mono text-xs uppercase tracking-wider text-amber transition-colors hover:bg-amber hover:text-bg disabled:cursor-not-allowed disabled:opacity-40"
        >
          {loading ? 'Fetching live data…' : '⟳ Refresh Live IPOs'}
        </button>
        {hasRun && !loading && predictions && (
          <span className="font-mono text-xs text-faint">
            {predictions.length} compan{predictions.length === 1 ? 'y' : 'ies'} currently open or recently listed
          </span>
        )}
      </div>

      {loading && (
        <div className="flex flex-col gap-5">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="rounded-lg border border-border bg-panel p-5 sm:p-6">
              <div className="h-4 w-1/3 animate-pulse rounded bg-panel-raised" />
              <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3">
                {Array.from({ length: 6 }).map((_, j) => (
                  <div key={j} className="h-8 animate-pulse rounded bg-panel-raised" />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && error && <ErrorPanel message={error} />}

      {!loading && !error && hasRun && predictions && predictions.length === 0 && (
        <p className="font-mono text-xs text-faint">
          No currently open or recently-listed IPOs found.
        </p>
      )}

      {!loading && !error && predictions && predictions.length > 0 && (
        <div className="flex flex-col gap-5">
          {predictions.map((entry) => (
            <LiveIpoCard key={entry.company_name} entry={entry} />
          ))}
        </div>
      )}

      {!loading && !error && !hasRun && (
        <p className="font-mono text-xs text-faint">
          Click refresh to fetch live GMP data and predictions for every currently open or recently-listed IPO.
        </p>
      )}
    </div>
  )
}
