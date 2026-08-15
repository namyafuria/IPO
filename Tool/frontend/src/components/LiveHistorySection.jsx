import { useState } from 'react'
import { getLiveHistory, ApiError } from '../api'

// Surfaces GET /ipos/{name}/live-history (routers_live.py), previously
// built but never wired into any UI. Exact column names in gmp_history /
// subscription_history / prediction_history vary by table and aren't all
// confirmed here, so rows are rendered generically (key: value pairs)
// rather than assuming specific fields -- this stays correct even if the
// underlying schema changes shape.
const HIDDEN_KEYS = new Set(['company_name', 'id'])

function formatRow(row) {
  return Object.entries(row)
    .filter(([k, v]) => !HIDDEN_KEYS.has(k) && v != null && v !== '')
    .map(([k, v]) => {
      const val = typeof v === 'object' ? JSON.stringify(v) : String(v)
      return `${k}: ${val}`
    })
    .join(' \u00b7 ')
}

function RowList({ title, rows, emptyLabel }) {
  const tail = rows.slice(-8).reverse() // most recent first, capped
  return (
    <div>
      <p className="font-mono text-[10px] uppercase tracking-wider text-faint">{title}</p>
      {tail.length === 0 ? (
        <p className="mt-1 font-mono text-[10px] text-faint">{emptyLabel}</p>
      ) : (
        <div className="mt-1 flex flex-col gap-1">
          {tail.map((row, i) => (
            <p key={i} className="num truncate font-mono text-[10px] text-muted">
              {formatRow(row)}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

export default function LiveHistorySection({ companyName }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('idle') // idle | loading | ready | error
  const [error, setError] = useState(null)

  async function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    if (data || status === 'loading') return
    setStatus('loading')
    setError(null)
    try {
      const result = await getLiveHistory(companyName)
      setData(result)
      setStatus('ready')
    } catch (err) {
      setStatus('error')
      setError(err instanceof ApiError ? err.message : 'Could not load history.')
    }
  }

  return (
    <div className="mt-3 border-t border-border pt-3">
      <button
        type="button"
        onClick={toggle}
        className="font-mono text-[10px] uppercase tracking-wider text-muted transition-colors hover:text-amber"
      >
        {open ? 'Hide history \u25b4' : 'View history \u25be'}
      </button>

      {open && status === 'loading' && (
        <div className="mt-2 h-12 animate-pulse rounded bg-panel-raised" />
      )}

      {open && status === 'error' && (
        <p className="mt-2 font-mono text-[10px] text-loss/80">{error}</p>
      )}

      {open && status === 'ready' && data && (
        <div className="mt-3 flex flex-col gap-3">
          <RowList title="GMP" rows={data.gmp_history ?? []} emptyLabel="No GMP history yet." />
          <RowList
            title="Subscription"
            rows={data.subscription_history ?? []}
            emptyLabel="No subscription history yet."
          />
          <RowList
            title="Predictions over time"
            rows={data.prediction_history ?? []}
            emptyLabel="No predictions recorded yet."
          />
        </div>
      )}
    </div>
  )
}
