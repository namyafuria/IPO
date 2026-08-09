import { Section, Field } from './DataField'
import Badge from './Badge'
import { fmtCr, fmtPct, fmtDate, gainClass } from '../format'

export default function CompanyPanel({ data, onRefresh, refreshing }) {
  const { record, exact_match, source } = data

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] rounded-lg border border-border bg-panel p-5 sm:p-6">
      <style>{`@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }`}</style>

      {/* Header */}
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3 border-b border-border pb-5">
        <div>
          <h2 className="font-display text-xl font-medium text-ink sm:text-2xl">{record.company_name}</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {record.issue_category && <Badge tone="amber">{record.issue_category}</Badge>}
            {record.sector && <Badge>{record.sector}</Badge>}
            {!exact_match && <Badge tone="loss">closest match</Badge>}
            {source === 'live_fetch' && <Badge tone="gain">fetched live</Badge>}
          </div>
        </div>
        <div className="flex items-start gap-3">
          {record.listing_day_gain_pct !== null && record.listing_day_gain_pct !== undefined && (
            <div className="text-right">
              <div className="text-xs text-muted">Listing-day gain</div>
              <div className={`num text-xl font-medium sm:text-2xl ${gainClass(record.listing_day_gain_pct)}`}>
                {fmtPct(record.listing_day_gain_pct)}
              </div>
            </div>
          )}
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              disabled={refreshing}
              title="Re-fetch this company's data"
              className="mt-0.5 rounded-md border border-border bg-panel-raised px-2.5 py-1.5 font-mono text-[11px] uppercase tracking-wider text-muted transition-colors hover:border-amber-dim hover:text-amber disabled:cursor-not-allowed disabled:opacity-40"
            >
              {refreshing ? '…' : '↻ Refresh'}
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-5">
        <Section title="Issue overview">
          <Field label="Issue size" value={fmtCr(record.issue_size_cr)} />
          <Field label="Price band (upper)" value={record.price_band_upper != null ? `₹${record.price_band_upper}` : '—'} />
          <Field label="PE ratio" value={record.pe_ratio ?? '—'} />
          <Field label="ROE" value={fmtPct(record.roe, 1)} />
          <Field label="Debt / Equity" value={record.debt_equity ?? '—'} />
          <Field label="Anchor allocation" value={fmtPct(record.anchor_allocation_pct, 1)} />
        </Section>

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

        {(record.price_day1 || record.price_day2 || record.price_day5 || record.price_day10) && (
          <Section title="Post-listing close price">
            <Field label="Day 1" value={record.price_day1 != null ? `₹${record.price_day1}` : '—'} />
            <Field label="Day 2" value={record.price_day2 != null ? `₹${record.price_day2}` : '—'} />
            <Field label="Day 3" value={record.price_day3 != null ? `₹${record.price_day3}` : '—'} />
            <Field label="Day 5" value={record.price_day5 != null ? `₹${record.price_day5}` : '—'} />
            <Field label="Day 10" value={record.price_day10 != null ? `₹${record.price_day10}` : '—'} />
          </Section>
        )}

        {(record.current_price != null) && (
          <Section title="Live tracking">
            <Field label="Current price" value={`₹${record.current_price}`} />
            <Field label="Current gain" value={fmtPct(record.current_gain_pct)} valueClassName={gainClass(record.current_gain_pct)} />
            <Field label="As of" value={fmtDate(record.current_price_asof)} />
          </Section>
        )}

        <Section title="Identifiers">
          <Field label="ISIN" value={record.isin || '—'} />
          <Field label="BSE code" value={record.bse_script_code || '—'} />
          <Field label="NSE symbol" value={record.nse_symbol || '—'} />
        </Section>
      </div>
    </div>
  )
}
