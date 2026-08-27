import { useEffect, useState, useCallback } from 'react'
import Badge from './Badge'
import LiveHistorySection from './LiveHistorySection'
import CompanyDetailSection from './CompanyDetailSection'
import { getOpenIpos, ApiError } from '../api'
import { fmtProb } from '../format'

// Step 8. The backend's own scheduler polls open IPOs hourly and writes
// live_predictions itself (see routers_live.py) -- this panel just reads
// whatever's current. The refresh button re-fetches that snapshot; it does
// NOT trigger a new live poll (that's the backend's job, on its own hourly
// cadence), so a click here can legitimately show the same numbers as
// before if less than an hour has passed.
// FIX (2026-08-16): live_predict.py's _run_model() builds `buckets` as a
// LIST of {label, probability, most_likely} objects (see save_prediction(),
// which json.dumps()'s result["prediction"]["buckets"] straight into the
// bucket_probabilities column) -- never a plain {label: probability} dict.
// The old Object.entries()-based version treated a parsed JSON array as if
// it were that dict shape: entries came out as [["0", {...}], ["1", {...}]]
// (array index as key), so the "best" pick compared whole objects as
// probabilities and rendered as "0 · NaN%" for every prediction. This reads
// the real shape directly: prefer the entry already flagged most_likely
// (matches _run_model's own top_i), falling back to a max-by-probability
// scan only if that flag is ever absent.
function bestBucket(bucketProbabilities) {
  if (!Array.isArray(bucketProbabilities) || bucketProbabilities.length === 0) return null
  const flagged = bucketProbabilities.find((b) => b.most_likely)
  if (flagged) return [flagged.label, flagged.probability]
  const top = bucketProbabilities.reduce((best, cur) =>
    (cur.probability ?? -Infinity) > (best.probability ?? -Infinity) ? cur : best
  )
  return [top.label, top.probability]
}

function fmtMultiple(x) {
  return x == null ? '—' : `${Number(x).toFixed(2)}x`
}

export default function OpenIposPanel() {
  const [ipos, setIpos] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async (isRefresh = false) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      const data = await getOpenIpos()
      setIpos(data.ipos ?? [])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load open IPOs.')
    } finally {
      isRefresh ? setRefreshing(false) : setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(false)
  }, [load])

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-2">
        <p className="font-mono text-xs text-muted">
          {ipos.length > 0 ? `${ipos.length} currently open` : 'Currently open for bidding'}
        </p>
        <button
          type="button"
          onClick={() => load(true)}
          disabled={refreshing}
          className="font-mono text-xs uppercase tracking-wider text-muted transition-colors hover:text-amber disabled:opacity-50"
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {loading && (
        <div className="flex flex-col gap-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-20 animate-pulse rounded-lg bg-panel-raised" />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="rounded-md border border-loss/30 bg-loss/5 p-4">
          <p className="text-sm text-ink/90">{error}</p>
        </div>
      )}

      {!loading && !error && ipos.length === 0 && (
        <p className="font-mono text-xs text-faint">Nothing currently open for bidding.</p>
      )}

      {!loading && !error && ipos.length > 0 && (
        <div className="flex flex-col gap-3">
          {ipos.map((ipo) => {
            const top = bestBucket(ipo.latest_prediction?.bucket_probabilities)
            return (
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
                      {ipo.sector ? ` · ${ipo.sector}` : ''}
                    </p>
                  </div>
                  <Badge tone="neutral">{ipo.status ?? 'open'}</Badge>
                </div>

                <div className="mt-3 grid grid-cols-3 gap-3 border-t border-border pt-3">
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Sub.</p>
                    <p className="num text-sm text-ink">{fmtMultiple(ipo.current_subscription_total)}</p>
                  </div>
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-wider text-faint">GMP</p>
                    <p className="num text-sm text-ink">
                      {ipo.current_gmp_percent != null ? `${ipo.current_gmp_percent}%` : '—'}
                    </p>
                  </div>
                  <div>
                    <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Closes</p>
                    <p className="num text-sm text-ink">{ipo.close_date ?? '—'}</p>
                  </div>
                </div>

                {top ? (
                  <div className="mt-3 flex items-center justify-between border-t border-border pt-3">
                    <span className="font-mono text-xs text-muted">Predicted Day 1</span>
                    <span className="num text-sm font-medium text-amber">
                      {top[0]} · {fmtProb(top[1])}
                    </span>
                  </div>
                ) : (
                  <p className="mt-3 border-t border-border pt-3 font-mono text-[10px] text-faint">
                    No prediction yet — waiting on subscription data.
                  </p>
                )}

                {ipo.as_of && (
                  <p className="mt-2 font-mono text-[10px] text-faint">as of {ipo.as_of}</p>
                )}

                <LiveHistorySection companyName={ipo.company_name} />
                <CompanyDetailSection companyName={ipo.company_name} />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
