export default function Badge({ children, tone = 'neutral' }) {
  const tones = {
    neutral: 'border-border text-muted',
    amber: 'border-amber-dim text-amber',
    gain: 'border-gain/40 text-gain',
    loss: 'border-loss/40 text-loss',
  }
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider ${tones[tone]}`}
    >
      {children}
    </span>
  )
}
