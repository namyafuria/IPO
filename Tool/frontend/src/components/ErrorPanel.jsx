export default function ErrorPanel({ message }) {
  return (
    <div className="rounded-lg border border-loss/30 bg-loss/5 p-5">
      <div className="font-mono text-[11px] uppercase tracking-wider text-loss">No result</div>
      <p className="mt-2 text-sm text-ink/90">{message}</p>
    </div>
  )
}
