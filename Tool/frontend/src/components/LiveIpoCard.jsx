import { Section, Field } from './DataField'
import Badge from './Badge'
import BucketBar from './BucketBar'
import { fmtPct, fmtDate, gainClass } from '../format'

// Same shape PredictionPanel already relies on (app/predict.py):
// { prediction: { buckets, model_stats }, issue_category, inputs_used }
function extractGain(gain) {
  if (!gain) return { buckets: [] }
  const pred = gain.prediction ?? {}
  return { buckets: pred.buckets ?? [] }
}

// Same shape TrajectoryPanel already relies on (app/predict_trajectory.py):
// { horizons: { day2: {...}, day3: {...}, ... } }
function extractHorizons(trajectory) {
  const raw = trajectory?.horizons ?? {}
  return Object.entries(raw)
    .map(([key, h]) => ({
      key,
      label: `Day ${key.replace(/\D/g, '')}`,
      day: Number(key.replace(/\D/g, '')),
      buckets: h.buckets ?? [],
      reliable: h.reliable,
    }))
    .sort((a, b) => a.day - b.day)
}

export default function LiveIpoCard({ entry }) {
  const { company_name, record, gain, gain_error, trajectory, trajectory_error } = entry
  const gainBuckets = extractGain(gain).buckets
  const horizons = extractHorizons(trajectory)

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] rounded-lg border border-border bg-panel p-5 sm:p-6">
      {/* Header */}
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
        <div>
          <h3 className="font-display text-lg font-medium text-ink">{company_name}</h3>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {record?.issue_category && <Badge tone="amber">{record.issue_category}</Badge>}
            {record?.sector && <Badge>{record.sector}</Badge>}
          </div>
        </div>
        {record?.listing_day_gain_pct !== null && record?.listing_day_gain_pct !== undefined && (
          <div className="text-right">
            <div className="text-xs text-muted">Listing-day gain</div>
            <div className={`num text-lg font-medium ${gainClass(record.listing_day_gain_pct)}`}>
              {fmtPct(record.listing_day_gain_pct)}
            </div>
          </div>
        )}
      </div>

      {/* Live data */}
      {record ? (
        <div className="flex flex-col gap-4">
          <Section title="Subscription & GMP">
            <Field label="QIB" value={record.subscription_qib != null ? `${record.subscription_qib}×` : '—'} />
            <Field label="HNI" value={record.subscription_hni != null ? `${record.subscription_hni}×` : '—'} />
            <Field label="RII" value={record.subscription_rii != null ? `${record.subscription_rii}×` : '—'} />
            <Field label="Total" value={record.subscription_total != null ? `${record.subscription_total}×` : '—'} valueClassName="text-amber" />
            <Field label="GMP" value={fmtPct(record.gmp_percent, 1)} valueClassName={gainClass(record.gmp_percent)} />
          </Section>
          <Section title="Timeline">
            <Field label="Open" value={fmtDate(record.open_date)} />
            <Field label="Close" value={fmtDate(record.close_date)} />
            <Field label="Allotment" value={fmtDate(record.allotment_date)} />
            <Field label="Listing" value={fmtDate(record.listing_date)} />
          </Section>
        </div>
      ) : (
        <p className="mb-4 font-mono text-xs text-faint">No live record found for this company.</p>
      )}

      {/* Gain prediction */}
      <div className="mt-5 border-t border-border pt-4">
        <h4 className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-faint">
          Listing-day gain prediction
        </h4>
        {gain_error && <p className="font-mono text-xs text-loss">{gain_error}</p>}
        {!gain_error && gainBuckets.length > 0 && <BucketBar buckets={gainBuckets} />}
        {!gain_error && gainBuckets.length === 0 && (
          <p className="font-mono text-xs text-faint">No prediction available.</p>
        )}
      </div>

      {/* Trajectory */}
      <div className="mt-5 border-t border-border pt-4">
        <h4 className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-faint">
          Post-listing trajectory
        </h4>
        {trajectory_error && <p className="font-mono text-xs text-loss">{trajectory_error}</p>}
        {!trajectory_error && horizons.length > 0 && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {horizons.map((h) => (
              <div key={h.key} className="rounded-lg border border-border bg-panel-raised p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="font-mono text-xs uppercase tracking-wider text-muted">{h.label}</span>
                  {h.reliable === false && <Badge tone="loss">low reliability</Badge>}
                </div>
                {h.buckets.length > 0 ? (
                  <BucketBar buckets={h.buckets} />
                ) : (
                  <p className="font-mono text-xs text-faint">No data for this horizon.</p>
                )}
              </div>
            ))}
          </div>
        )}
        {!trajectory_error && horizons.length === 0 && (
          <p className="font-mono text-xs text-faint">No trajectory available yet.</p>
        )}
      </div>
    </div>
  )
}
