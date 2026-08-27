import { useEffect, useState, useCallback } from 'react'
import Badge from './Badge'
import { getUpcomingIpos, ApiError } from '../api'

// Companion to OpenIposPanel. /ipos/upcoming (routers_live.py, added
// 2026-08-27) is the flip side of /ipos/open's new open_date filter --
// companies ipoji.com already lists as "current" but that haven't started
// bidding yet. No subscription/GMP/prediction fields exist for these by
// definition (bidding hasn't opened), so this deliberately shows only
// roster + basic terms rather than rendering "—" placeholders everywhere
// like the old blended list did.
function fmtIssueSize(cr) {
  return cr == null ? '—' : `₹${Number(cr).toFixed(1)} Cr`
}

export default function UpcomingIposPanel() {
  const [ipos, setIpos] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async (isRefresh = false) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      const data = await getUpcomingIpos()
      setIpos(data.ipos ?? [])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load upcoming IPOs.')
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
          {ipos.length > 0 ? `${ipos.length} opening soon` : 'Announced, not yet open'}
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
        <p className="font-mono text-xs text-faint">Nothing announced ahead of the currently-open list.</p>
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
                    {ipo.sector ? ` · ${ipo.sector}` : ''}
                  </p>
                </div>
                <Badge tone="neutral">opens {ipo.open_date}</Badge>
              </div>

              <div className="mt-3 grid grid-cols-3 gap-3 border-t border-border pt-3">
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Price band</p>
                  <p className="num text-sm text-ink">
                    {ipo.price_band_upper != null ? `₹${ipo.price_band_upper}` : '—'}
                  </p>
                </div>
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Issue size</p>
                  <p className="num text-sm text-ink">{fmtIssueSize(ipo.issue_size_cr)}</p>
                </div>
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-wider text-faint">Closes</p>
                  <p className="num text-sm text-ink">{ipo.close_date ?? '—'}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
