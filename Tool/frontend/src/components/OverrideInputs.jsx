import { useState } from 'react'

export default function OverrideInputs({ defaultSubscription, defaultGmp, onApply, loading }) {
  const [subscription, setSubscription] = useState(defaultSubscription ?? '')
  const [gmp, setGmp] = useState(defaultGmp ?? '')

  function handleSubmit(e) {
    e.preventDefault()
    onApply({
      subscription: subscription === '' ? undefined : Number(subscription),
      gmp: gmp === '' ? undefined : Number(gmp),
    })
  }

  function handleReset() {
    setSubscription('')
    setGmp('')
    onApply({ subscription: undefined, gmp: undefined })
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1">
        <span className="font-mono text-[11px] uppercase tracking-wider text-faint">
          Subscription (×)
        </span>
        <input
          type="number"
          step="0.01"
          min="0"
          value={subscription}
          onChange={(e) => setSubscription(e.target.value)}
          placeholder="e.g. 12.5"
          className="num w-28 rounded-md border border-border bg-panel-raised px-2.5 py-1.5 text-sm text-ink placeholder:text-faint focus:border-amber-dim focus:outline-none"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="font-mono text-[11px] uppercase tracking-wider text-faint">GMP (%)</span>
        <input
          type="number"
          step="0.01"
          value={gmp}
          onChange={(e) => setGmp(e.target.value)}
          placeholder="e.g. 18"
          className="num w-28 rounded-md border border-border bg-panel-raised px-2.5 py-1.5 text-sm text-ink placeholder:text-faint focus:border-amber-dim focus:outline-none"
        />
      </label>
      <button
        type="submit"
        disabled={loading}
        className="rounded-md border border-border bg-panel-raised px-3 py-1.5 font-mono text-xs uppercase tracking-wider text-muted transition-colors hover:border-amber-dim hover:text-amber disabled:cursor-not-allowed disabled:opacity-40"
      >
        {loading ? 'Predicting…' : 'Re-run with these'}
      </button>
      {(subscription !== '' || gmp !== '') && (
        <button
          type="button"
          onClick={handleReset}
          disabled={loading}
          className="font-mono text-xs text-faint underline decoration-dotted underline-offset-4 hover:text-muted disabled:cursor-not-allowed disabled:opacity-40"
        >
          reset to actual
        </button>
      )}
    </form>
  )
}
