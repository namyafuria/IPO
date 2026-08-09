export function Section({ title, children }) {
  return (
    <div className="border-t border-border pt-4 first:border-t-0 first:pt-0">
      <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-faint">
        {title}
      </h3>
      <div className="grid grid-cols-2 gap-x-6 gap-y-2.5 sm:grid-cols-3">{children}</div>
    </div>
  )
}

export function Field({ label, value, valueClassName = '' }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted">{label}</span>
      <span className={`num text-sm text-ink ${valueClassName}`}>{value}</span>
    </div>
  )
}
