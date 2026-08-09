export function fmtNum(v, opts = {}) {
  if (v === null || v === undefined) return '—'
  const { decimals = 2, prefix = '', suffix = '' } = opts
  return `${prefix}${Number(v).toFixed(decimals)}${suffix}`
}

export function fmtCr(v) {
  if (v === null || v === undefined) return '—'
  return `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: 1 })} Cr`
}

export function fmtPct(v, decimals = 2) {
  if (v === null || v === undefined) return '—'
  return `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(decimals)}%`
}

export function fmtDate(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function gainClass(v) {
  if (v === null || v === undefined) return 'text-muted'
  return Number(v) >= 0 ? 'text-gain' : 'text-loss'
}

export function fmtProb(v, decimals = 1) {
  if (v === null || v === undefined) return '—'
  // Accept either a 0-1 fraction or an already-scaled 0-100 percentage.
  const n = Number(v)
  const pct = n <= 1 ? n * 100 : n
  return `${pct.toFixed(decimals)}%`
}
