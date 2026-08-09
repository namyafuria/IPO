import { useState } from 'react'

export default function SearchBar({ onSearch, loading }) {
  const [value, setValue] = useState('')

  function handleSubmit(e) {
    e.preventDefault()
    const trimmed = value.trim()
    if (trimmed) onSearch(trimmed)
  }

  return (
    <form onSubmit={handleSubmit} className="w-full">
      <div className="group relative flex items-center rounded-lg border border-border bg-panel transition-colors focus-within:border-amber-dim">
        <span className="pl-4 font-mono text-sm text-faint select-none">→</span>
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Search a company — e.g. Ola Electric, Swiggy, Vishal Mega Mart"
          className="w-full bg-transparent px-3 py-4 font-body text-base text-ink placeholder:text-faint focus:outline-none"
          autoFocus
        />
        <button
          type="submit"
          disabled={loading || !value.trim()}
          className="mr-2 rounded-md border border-border bg-panel-raised px-4 py-2 font-mono text-xs uppercase tracking-wider text-muted transition-colors hover:border-amber-dim hover:text-amber disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-border disabled:hover:text-muted"
        >
          {loading ? 'Looking…' : 'Look up'}
        </button>
      </div>
    </form>
  )
}
