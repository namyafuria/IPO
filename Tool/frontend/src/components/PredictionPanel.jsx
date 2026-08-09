import { useEffect, useState, useCallback } from 'react'
import BucketBar from './BucketBar'
import OverrideInputs from './OverrideInputs'
import Badge from './Badge'
import { getPrediction, ApiError } from '../api'
import { fmtProb, fmtPct, gainClass } from '../format'

// Confirmed against the real backend (app/predict.py, 2026-08-09): the
// response nests everything under `prediction`, and accuracy figures live
// under `prediction.model_stats`, not top-level.
function extract(data) {
  const pred = data.prediction ?? {}
  return {
    buckets: pred.buckets ?? [],
    modelAcc: pred.model_stats?.validated_top_bucket_accuracy,
    naiveAcc: pred.model_stats?.validated_naive_top_bucket_accuracy,
  }
}

export default function PredictionPanel({
  companyName,
  defaultSubscription,
  defaultGmp,
  actualGainPct,
  subscriptionOverride,
  gmpOverride,
  onOverrideChange,
}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const runPredict = useCallback(
    async (overrides = {}) => {
      setLoading(true)
      setError(null)
      try {
        const result = await getPrediction(companyName, overrides)
        setData(result)
      } catch (err) {
        setData(null)
        setError(err instanceof ApiError ? err.message : 'Could not get a prediction.')
      } finally {
        setLoading(false)
      }
    },
    [companyName]
  )

  // Re-run whenever the company changes, or whenever the shared override
  // values change (e.g. this panel's own "re-run with these" click, which
  // now also propagates up to App via onOverrideChange).
  useEffect(() => {
    runPredict({ subscription: subscriptionOverride, gmp: gmpOverride })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyName, subscriptionOverride, gmpOverride])

  const handleApply = useCallback(
    (overrides) => {
      onOverrideChange?.(overrides.subscription, overrides.gmp)
    },
    [onOverrideChange]
  )

  const buckets = data ? extract(data).buckets : []
  const category = data?.issue_category
  const modelAcc = data ? extract(data).modelAcc : undefined
  const naiveAcc = data ? extract(data).naiveAcc : undefined
  const subProvisional = data?.inputs_used?.subscription_provisional
  const gmpProvisional = data?.inputs_used?.gmp_provisional

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] rounded-lg border border-border bg-panel p-5 sm:p-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <h3 className="font-display text-lg font-medium text-ink">Listing-day gain prediction</h3>
          {category && <Badge tone="amber">{category}</Badge>}
          {(subProvisional || gmpProvisional) && <Badge tone="loss">provisional</Badge>}
        </div>
        {actualGainPct !== null && actualGainPct !== undefined && (
          <div className="text-right">
            <div className="font-mono text-[11px] uppercase tracking-wider text-faint">
              Actual outcome
            </div>
            <div className={`num text-sm font-medium ${gainClass(actualGainPct)}`}>
              {fmtPct(actualGainPct)}
            </div>
          </div>
        )}
      </div>

      <div className="mb-5">
        <OverrideInputs
          defaultSubscription={defaultSubscription}
          defaultGmp={defaultGmp}
          onApply={handleApply}
          loading={loading}
        />
      </div>

      {loading && (
        <div className="flex flex-col gap-2">
          <div className="h-3 w-full animate-pulse rounded-full bg-panel-raised" />
          <div className="h-3 w-3/4 animate-pulse rounded bg-panel-raised" />
        </div>
      )}

      {!loading && error && (
        <div className="rounded-md border border-loss/30 bg-loss/5 p-4">
          <p className="text-sm text-ink/90">{error}</p>
        </div>
      )}

      {!loading && !error && buckets.length > 0 && (
        <>
          <BucketBar buckets={buckets} />
          {(modelAcc != null || naiveAcc != null) && (
            <p className="mt-5 border-t border-border pt-4 font-mono text-[11px] text-faint">
              Model accuracy {modelAcc != null ? fmtProb(modelAcc) : '—'}
              {naiveAcc != null && <> vs {fmtProb(naiveAcc)} naive baseline</>}
            </p>
          )}
        </>
      )}

      {!loading && !error && buckets.length === 0 && (
        <p className="font-mono text-xs text-faint">No bucket data in the response.</p>
      )}
    </div>
  )
}
