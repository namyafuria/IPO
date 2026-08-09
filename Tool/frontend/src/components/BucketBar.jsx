import { fmtProb } from '../format'

const LOSS = [229, 72, 77] // #E5484D
const AMBER = [212, 167, 61] // #D4A73D
const GAIN = [63, 191, 127] // #3FBF7F

function lerp(a, b, t) {
  return Math.round(a + (b - a) * t)
}

function rampColor(t) {
  const [r, g, b] =
    t <= 0.5
      ? [
          lerp(LOSS[0], AMBER[0], t / 0.5),
          lerp(LOSS[1], AMBER[1], t / 0.5),
          lerp(LOSS[2], AMBER[2], t / 0.5),
        ]
      : [
          lerp(AMBER[0], GAIN[0], (t - 0.5) / 0.5),
          lerp(AMBER[1], GAIN[1], (t - 0.5) / 0.5),
          lerp(AMBER[2], GAIN[2], (t - 0.5) / 0.5),
        ]
  return `rgb(${r}, ${g}, ${b})`
}

// buckets: ordered ascending by outcome, e.g.
// [{ label: 'Loss', probability: 0.484 }, { label: '0-10%', probability: 0.253 }, ...]
export default function BucketBar({ buckets }) {
  if (!buckets || buckets.length === 0) return null

  const n = buckets.length
  const maxProb = Math.max(...buckets.map((b) => Number(b.probability) || 0))
  const withColor = buckets.map((b, i) => ({
    ...b,
    color: rampColor(n === 1 ? 0.5 : i / (n - 1)),
    isTop: (Number(b.probability) || 0) === maxProb,
  }))

  return (
    <div>
      {/* segmented bar */}
      <div className="flex h-3 w-full overflow-hidden rounded-full border border-border">
        {withColor.map((b, i) => {
          const pct = Math.max(Number(b.probability) || 0, 0) * (Number(b.probability) <= 1 ? 100 : 1)
          return (
            <div
              key={i}
              title={`${b.label}: ${fmtProb(b.probability)}`}
              style={{
                width: `${pct}%`,
                backgroundColor: b.color,
                opacity: b.isTop ? 1 : 0.55,
              }}
              className="h-full transition-opacity first:rounded-l-full last:rounded-r-full"
            />
          )
        })}
      </div>

      {/* legend */}
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
        {withColor.map((b, i) => (
          <div key={i} className="flex items-center gap-2">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: b.color, opacity: b.isTop ? 1 : 0.55 }}
            />
            <span className={`font-mono text-xs ${b.isTop ? 'text-ink' : 'text-muted'}`}>
              {b.label}
            </span>
            <span className={`num ml-auto text-xs ${b.isTop ? 'text-ink font-medium' : 'text-muted'}`}>
              {fmtProb(b.probability)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
