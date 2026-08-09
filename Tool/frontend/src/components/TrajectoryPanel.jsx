import { useEffect, useState, useCallback } from 'react'
import BucketBar from './BucketBar'
import Badge from './Badge'
import { getTrajectory, ApiError } from '../api'
import { fmtProb, fmtPct, gainClass } from '../format'

// Confirmed against the real backend (app/predict_trajectory.py, 2026-08-09):
// `horizons` is a dict keyed "day2"/"day3"/"day5"/"day10", each holding
// { buckets, top_bucket, target_definition, reliable, reliability_note,
// model_stats: { validated_top_bucket_accuracy, validated_naive_top_bucket_accuracy, ... } }.
// Reliable horizons per current training: Mainboard day2/day3/day5 only —
// Mainboard day10 and every SME horizon are flagged unreliable by the API
// itself via `reliable`, so the UI just reads that field rather than
// hardcoding which horizons are which.
function normalizeHorizons(data) {
  const raw = data.horizons ?? {}
  return Object.entries(raw).map(([key, h]) => ({
    day: h.day ?? Number(key.replace(/\D/g, '')),
    label: `Day ${key.replace(/\D/g, '')}`,
    buckets: h.buckets ?? [],
    reliable: h.reliable,
    reliabilityNote: h.reliability_note,
    modelAcc: h.model_stats?.validated_top_bucket_accuracy,
    naiveAcc: h.model_stats?.validated_naive_top_bucket_accuracy,
  }))
}

// Actual outcome for a given horizon, computed from the company record already
// in hand (no extra API call needed). Basis is the listing-day close
// (price_day1), matching the "% change from listing-day price" convention
// the project settled on for Problem B.
function actualForDay(record, day) {
  if (!record) return null
  const dayPrice = record[`price_day${day}`]
  const basisPrice = record.price_day1
  if (dayPrice == null || basisPrice == null || basisPrice === 0) return null
  return { price: dayPrice, gainPct: ((dayPrice - basisPrice) / basisPrice) * 100 }
}

export default function TrajectoryPanel({ companyName, defaultSubscription, subscriptionOverride, gmpOverride, record }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const run = useCallback(
    async (overrides = {}) => {
      setLoading(true)
      setError(null)
      try {
        const result = await getTrajectory(companyName, overrides)
        setData(result)
      } catch (err) {
        setData(null)
        setError(err instanceof ApiError ? err.message : 'Could not get a trajectory prediction.')
      } finally {
        setLoading(false)
      }
    },
    [companyName]
  )

  // Re-run whenever the company changes, or whenever either shared
  // override (lifted up to App, set from the listing-day panel's
  // "re-run with these") changes. Falls back to the record's stored
  // subscription_total when no override is active; gmp has no stored
  // fallback here since the backend already applies its own fallback
  // to record.gmp_percent when gmp is omitted.
  useEffect(() => {
    run({ subscription: subscriptionOverride ?? defaultSubscription, gmp: gmpOverride })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyName, subscriptionOverride, gmpOverride])

  const horizons = data ? normalizeHorizons(data).sort((a, b) => a.day - b.day) : []
  const subProvisional = data?.inputs_used?.subscription_provisional

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] rounded-lg border border-border bg-panel p-5 sm:p-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-2 border-b border-border pb-4">
        <div>
          <h3 className="font-display text-lg font-medium text-ink">Post-listing price trajectory</h3>
          <p className="mt-1 text-xs text-muted">
            Bucket probabilities for each trading day after listing, relative to the day1 close.
          </p>
        </div>
        {subProvisional && <Badge tone="loss">provisional</Badge>}
      </div>

      {loading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-24 animate-pulse rounded-lg bg-panel-raised" />
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="rounded-md border border-loss/30 bg-loss/5 p-4">
          <p className="text-sm text-ink/90">{error}</p>
        </div>
      )}

      {!loading && !error && horizons.length === 0 && (
        <p className="font-mono text-xs text-faint">
          No trajectory available yet — bidding may still be open, or this issue hasn't listed.
        </p>
      )}

      {!loading && !error && horizons.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {horizons.map((h, i) => {
            const actual = actualForDay(record, h.day)
            return (
              <div key={i} className="rounded-lg border border-border bg-panel-raised p-4">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <span className="font-mono text-xs uppercase tracking-wider text-muted">
                    {h.label}
                  </span>
                  <div className="flex items-center gap-2">
                    {actual && (
                      <span className={`num text-xs font-medium ${gainClass(actual.gainPct)}`}>
                        actual {fmtPct(actual.gainPct)}
                      </span>
                    )}
                    {h.reliable === false && <Badge tone="loss">low reliability</Badge>}
                  </div>
                </div>

                {h.buckets.length > 0 ? (
                  <BucketBar buckets={h.buckets} />
                ) : (
                  <p className="font-mono text-xs text-faint">No data for this horizon.</p>
                )}

                {h.reliable === false && (
                  <p className="mt-3 text-xs leading-relaxed text-loss/80">
                    {h.reliabilityNote ||
                      "This horizon's model doesn't reliably beat a naive guess — treat it as a rough signal, not a forecast."}
                  </p>
                )}

                {(h.modelAcc != null || h.naiveAcc != null) && (
                  <p className="mt-3 border-t border-border pt-2 font-mono text-[10px] text-faint">
                    {h.modelAcc != null ? fmtProb(h.modelAcc) : '—'}
                    {h.naiveAcc != null && <> vs {fmtProb(h.naiveAcc)} naive</>}
                  </p>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
