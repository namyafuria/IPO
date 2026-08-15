import { useState } from 'react'
import SearchBar from './components/SearchBar'
import CompanyPanel from './components/CompanyPanel'
import PredictionPanel from './components/PredictionPanel'
import TrajectoryPanel from './components/TrajectoryPanel'
import ErrorPanel from './components/ErrorPanel'
import LiveIposPanel from './components/LiveIposPanel'
import OpenIposPanel from './components/OpenIposPanel'
import ListedIposPanel from './components/ListedIposPanel'
import { getCompany, refreshCompany, ApiError } from './api'

export default function App() {
  const [view, setView] = useState('search') // 'search' | 'live'
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [searched, setSearched] = useState(false)
  const [subscriptionOverride, setSubscriptionOverride] = useState(null)
  const [gmpOverride, setGmpOverride] = useState(null)

  async function handleSearch(name) {
    setLoading(true)
    setError(null)
    setSearched(true)
    try {
      const data = await getCompany(name)
      setResult(data)
      setSubscriptionOverride(null)
      setGmpOverride(null)
    } catch (err) {
      setResult(null)
      setError(err instanceof ApiError ? err.message : 'Something went wrong.')
    } finally {
      setLoading(false)
    }
  }

  async function handleRefresh() {
    if (!result?.record?.company_name) return
    setRefreshing(true)
    try {
      const data = await refreshCompany(result.record.company_name)
      setResult(data)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not refresh this company.')
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="min-h-screen">
      <div className="mx-auto flex max-w-2xl flex-col px-4 pb-24 pt-12 sm:px-6 sm:pt-24">
        {/* Wordmark */}
        <div className="mb-10 flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-amber" />
          <span className="font-mono text-xs uppercase tracking-[0.2em] text-muted">
            IPO Analyser
          </span>
        </div>

        {/* Hero */}
        <h1 className="font-display text-3xl font-medium leading-tight text-ink sm:text-4xl">
          Look up any Indian IPO.
        </h1>
        <p className="mt-2 max-w-md text-sm leading-relaxed text-muted">
          Search a company to see its issue details, subscription levels, and — once listed —
          its actual listing-day performance.
        </p>

        {/* Tabs */}
        <div className="mt-8 flex items-center gap-1 border-b border-border">
          <button
            type="button"
            onClick={() => setView('search')}
            className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
              view === 'search'
                ? 'border-b-2 border-amber text-amber'
                : 'border-b-2 border-transparent text-muted hover:text-ink'
            }`}
          >
            Search
          </button>
          <button
            type="button"
            onClick={() => setView('live')}
            className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
              view === 'live'
                ? 'border-b-2 border-amber text-amber'
                : 'border-b-2 border-transparent text-muted hover:text-ink'
            }`}
          >
            Live IPOs
          </button>
          <button
            type="button"
            onClick={() => setView('open')}
            className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
              view === 'open'
                ? 'border-b-2 border-amber text-amber'
                : 'border-b-2 border-transparent text-muted hover:text-ink'
            }`}
          >
            Open
          </button>
          <button
            type="button"
            onClick={() => setView('listed')}
            className={`px-3 py-2 font-mono text-xs uppercase tracking-wider transition-colors ${
              view === 'listed'
                ? 'border-b-2 border-amber text-amber'
                : 'border-b-2 border-transparent text-muted hover:text-ink'
            }`}
          >
            Listed
          </button>
        </div>

        {view === 'search' && (
          <>
            <div className="mt-6">
              <SearchBar onSearch={handleSearch} loading={loading} />
            </div>

            {/* Results */}
            <div className="mt-8">
              {loading && (
                <div className="rounded-lg border border-border bg-panel p-5 sm:p-6">
                  <div className="h-4 w-1/3 animate-pulse rounded bg-panel-raised" />
                  <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3">
                    {Array.from({ length: 6 }).map((_, i) => (
                      <div key={i} className="h-8 animate-pulse rounded bg-panel-raised" />
                    ))}
                  </div>
                </div>
              )}
              {!loading && error && <ErrorPanel message={error} />}
              {!loading && !error && result && (
                <div className="flex flex-col gap-5 sm:gap-6">
                  <CompanyPanel data={result} onRefresh={handleRefresh} refreshing={refreshing} />
                  <PredictionPanel
                    companyName={result.record.company_name}
                    defaultSubscription={result.record.subscription_total}
                    defaultGmp={result.record.gmp_percent}
                    actualGainPct={result.record.listing_day_gain_pct}
                    subscriptionOverride={subscriptionOverride}
                    gmpOverride={gmpOverride}
                    onOverrideChange={(sub, gmp) => {
                      setSubscriptionOverride(sub)
                      setGmpOverride(gmp)
                    }}
                  />
                  <TrajectoryPanel
                    companyName={result.record.company_name}
                    defaultSubscription={result.record.subscription_total}
                    subscriptionOverride={subscriptionOverride}
                    gmpOverride={gmpOverride}
                    record={result.record}
                  />
                </div>
              )}
              {!loading && !error && !result && searched === false && (
                <p className="font-mono text-xs text-faint">
                  Try a recent name — Ola Electric, Swiggy, NSDL, Vishal Mega Mart…
                </p>
              )}
            </div>
          </>
        )}

        {view === 'live' && (
          <div className="mt-6">
            <LiveIposPanel />
          </div>
        )}

        {view === 'open' && (
          <div className="mt-6">
            <OpenIposPanel />
          </div>
        )}

        {view === 'listed' && (
          <div className="mt-6">
            <ListedIposPanel />
          </div>
        )}
      </div>
    </div>
  )
}
