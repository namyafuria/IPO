import { useState } from 'react'
import CompanyPanel from './CompanyPanel'
import { getCompany, ApiError } from '../api'

// Companion to LiveHistorySection -- same collapsed-by-default,
// fetch-on-first-expand pattern, but shows the full search-page detail
// view (CompanyPanel) instead of GMP/subscription history. Used by both
// OpenIposPanel and ListedIposPanel so a card's full Issue Overview / PE
// ratio / Identifiers etc. are one click away instead of only visible via
// a separate Search lookup.
export default function CompanyDetailSection({ companyName }) {
  const [expanded, setExpanded] = useState(false)
  const [data, setData] = useState(null)
  const [status, setStatus] = useState('idle') // idle | loading | ready | error
  const [errorMessage, setErrorMessage] = useState(null)

  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    // Only fetch the first time it's opened -- collapsing/re-expanding
    // afterward reuses what's already loaded, same as a normal accordion.
    if (next && status === 'idle') {
      setStatus('loading')
      getCompany(companyName)
        .then((d) => {
          setData(d)
          setStatus('ready')
        })
        .catch((err) => {
          setErrorMessage(err instanceof ApiError ? err.message : 'Could not load details.')
          setStatus('error')
        })
    }
  }

  return (
    <div className="mt-3 border-t border-border pt-3">
      <button
        type="button"
        onClick={toggle}
        className="font-mono text-xs uppercase tracking-wider text-muted transition-colors hover:text-amber"
      >
        {expanded ? '▲ Hide details' : '▼ View details'}
      </button>

      {expanded && (
        <div className="mt-3">
          {status === 'loading' && (
            <div className="h-40 animate-pulse rounded-lg bg-panel-raised" />
          )}
          {status === 'error' && (
            <p className="font-mono text-[10px] text-faint">{errorMessage}</p>
          )}
          {status === 'ready' && data && <CompanyPanel data={data} />}
        </div>
      )}
    </div>
  )
}
