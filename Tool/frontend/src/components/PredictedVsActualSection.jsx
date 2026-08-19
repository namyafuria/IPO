import { useState } from 'react'
import Badge from './Badge'
import { getPredictedVsActual, ApiError } from '../api'

// Item 3. Collapsible "View predicted vs actual" toggle on Listed cards --
// same lazy-fetch-on-first-expand pattern as LiveHistorySection, so it
// costs nothing until the user actually opens it. Reads
// GET /ipos/{name}/predicted-vs-actual (routers_predicted_vs_actual.py),
// which itself just re-reads the already-cached trajectory prediction --
// no new backend load added by adding this section.
function horizonLabel(key) {
  // keys look like "day2"/"day5"/"day10"
  const n = key.replace(/\D/g, '')
  return n ? `Day ${n}` : key
}

function Row({ horizonKey, data }) {
  if (data.status === 'pending') {
    return (
      <div className="flex items-center justify-between py-1.5">
        <span className="font-mono text-[10px] text-faint">{horizonLabel(horizonKey)}</span>
        <span className="font-mono text-[10px] text-faint">not resolved yet</span>
      </div>
    )
  }
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="font-mono text-[10px] text-faint">{horizonLabel(horizonKey)}</span>
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10px] text-muted">
          predicted {data.predicted_bucket ?? '—'}
        </span>
        <span className="num font-mono text-[10px] text-ink">
          actual {data.actual_pct != null ? `${data.actual_pct}%` : '—'}
        </span>
        <Badge tone={data.correct ? 'gain' : 'loss'}>
          {data.correct ? 'correct' : 'missed'}
        </Badge>
      </div>
    </div>
  )
}

export default function PredictedVsActualSection({ companyName }) {
  const [expanded, setExpanded] = useState(false)
  const [status, setStatus] = useState('idle') // idle | loading | ready | error
  const [data, setData] = useState(null)
  const [errorMessage, setErrorMessage] = useState(null)

  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    if (next && status === 'idle') {
      setStatus('loading')
      getPredictedVsActual(companyName)
        .then((res) => {
          setData(res)
          setStatus('ready')
        })
        .catch((err) => {
          setErrorMessage(err instanceof ApiError ? err.message : null)
          setStatus('error')
        })
    }
  }

  return (
    <div className="mt-2 border-t border-border pt-2">
      <button
        type="button"
        onClick={toggle}
        className="font-mono text-[10px] uppercase tracking-wider text-muted transition-colors hover:text-amber"
      >
        {expanded ? 'Hide predicted vs actual' : 'View predicted vs actual'}
      </button>

      {expanded && status === 'loading' && (
        <div className="mt-2 h-6 animate-pulse rounded bg-panel-raised" />
      )}

      {expanded && status === 'error' && (
        <p className="mt-2 font-mono text-[10px] text-faint">
          {errorMessage || 'Could not load predicted vs actual.'}
        </p>
      )}

      {expanded && status === 'ready' && data && (
        <div className="mt-1 flex flex-col divide-y divide-border/50">
          {Object.entries(data.horizons ?? {}).map(([key, val]) => (
            <Row key={key} horizonKey={key} data={val} />
          ))}
        </div>
      )}
    </div>
  )
}
