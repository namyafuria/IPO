import { useState } from 'react'
import OpenIposPanel from './OpenIposPanel'
import ListedIposPanel from './ListedIposPanel'

// Live IPOs = everything currently relevant right now, split into two
// sub-views that each read from their own already-fast, DB-backed
// endpoint (see routers_live.py):
//   - Open: GET /ipos/open -- rows in ipo_live_tracker, kept fresh by the
//     backend's own hourly poller. No live network call happens on click.
//   - Listed: GET /ipos/listed -- companies still inside their Day1-10
//     trajectory window, counted in real NSE trading sessions (not
//     calendar days), so the "proper criteria" for which companies
//     qualify is enforced server-side, not duplicated here.
//
// REPLACES the old version of this component, which called
// POST /api/sync_and_predict directly -- that endpoint synchronously
// loops predict_for_company()/predict_trajectory_for_company() over every
// tracked company (55+) in one request, routinely exceeding the 20-60s
// client timeout and showing a misleading "waking up from idle" message
// even when the backend was working correctly. Open/Listed above never
// had that problem -- they were already fast when tested directly.
export default function LiveIposPanel() {
  const [subView, setSubView] = useState('open') // 'open' | 'listed'

  return (
    <div>
      <div className="mb-6 flex items-center gap-1 border-b border-border">
        <button
          type="button"
          onClick={() => setSubView('open')}
          className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
            subView === 'open'
              ? 'border-b-2 border-amber text-amber'
              : 'border-b-2 border-transparent text-muted hover:text-ink'
          }`}
        >
          Open
        </button>
        <button
          type="button"
          onClick={() => setSubView('listed')}
          className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
            subView === 'listed'
              ? 'border-b-2 border-amber text-amber'
              : 'border-b-2 border-transparent text-muted hover:text-ink'
          }`}
        >
          Listed
        </button>
      </div>

      {subView === 'open' && <OpenIposPanel />}
      {subView === 'listed' && <ListedIposPanel />}
    </div>
  )
}
