import { memo, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { DayRow } from '../../protocol'
import { fmtCompact, fmtNum, fmtPct, INK, linePath, niceTicks } from './chartTheme'
import { ChartTip, useChartTip, type TipRow } from './ChartTip'

/** The VaultStats fields a day row carries (void/memory/vault_stats.py VAULT_KEYS). */
export type VaultMetricKey = 'notes' | 'bytes' | 'mean_note_chars' | 'link_density' | 'orphan_rate' | 'index_coverage'

/** One day's vault figures. A field is null where the server sent none: a rate of a vault without notes, or index
 *  coverage without an index note. */
export type VaultPoint = { day: number } & Record<VaultMetricKey, number | null>

export interface VaultLine {
  key: string
  label: string
  color: string
  /** In day order. */
  points: VaultPoint[]
  /**
   * A comparison line, drawn dashed and muted. It is drawn only on the per-note panels: the population's notes and
   * bytes are sums over every vault, so they share no scale with one agent's.
   */
  reference?: boolean
}

const finite = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function toPoint(day: number, v: unknown): VaultPoint | null {
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  return {
    day,
    notes: finite(o.notes),
    bytes: finite(o.bytes),
    mean_note_chars: finite(o.mean_note_chars),
    link_density: finite(o.link_density),
    orphan_rate: finite(o.orphan_rate),
    index_coverage: finite(o.index_coverage),
  }
}

/**
 * Day rows' vault figures in day order: the population's (`vault`) or, with an agent id, that agent's
 * (`vault_by_agent[id]`, present only while it was alive at the day's end). Days without them are left out.
 */
// oxlint-disable-next-line react/only-export-components
export function vaultPoints(daily: readonly DayRow[], agentId: string | null = null): VaultPoint[] {
  const out: VaultPoint[] = []
  for (const row of daily) {
    if (finite(row.day) === null) continue
    let v: unknown = row.vault
    if (agentId !== null) {
      const by = row.vault_by_agent
      v = by && typeof by === 'object' && Object.hasOwn(by, agentId) ? by[agentId] : null
    }
    const p = toPoint(row.day, v)
    if (p) out.push(p)
  }
  return out.sort((a, b) => a.day - b.day)
}

const fmtCount = (v: number): string => fmtCompact(Math.round(v))

function fmtBytes(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`
  if (Math.abs(v) >= 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${Math.round(v)} B`
}

interface MetricSpec {
  key: VaultMetricKey
  title: string
  fmt: (v: number) => string
  /** Fixed axis maximum (the rates); otherwise a nice value above the data. */
  max?: number
  /** A per-note figure, comparable between one vault and the pooled population. */
  perNote: boolean
  /** Shown when no day has a value. */
  none: string
}

const METRICS: readonly MetricSpec[] = [
  { key: 'notes', title: 'Notes', fmt: fmtCount, perNote: false, none: 'no notes yet' },
  { key: 'bytes', title: 'Bytes', fmt: fmtBytes, perNote: false, none: 'no notes yet' },
  { key: 'link_density', title: 'Links per note', fmt: (v) => fmtNum(v, 2), perNote: true, none: 'no notes yet' },
  { key: 'orphan_rate', title: 'Orphan rate', fmt: (v) => fmtPct(v), max: 1, perNote: true, none: 'no notes yet' },
  { key: 'index_coverage', title: 'Index coverage', fmt: (v) => fmtPct(v), max: 1, perNote: true, none: 'no index note yet' },
  { key: 'mean_note_chars', title: 'Mean note chars', fmt: fmtCount, perNote: true, none: 'no notes yet' },
]

const W = 170
const H = 66
const PAD_L = 3
const PAD_R = 8
const TOP = 12
const BOTTOM = 14

/** Path through the finite points; a null breaks the line. Isolated points come back separately for a dot. */
function segments(pts: Array<[number, number] | null>): { d: string; dots: Array<[number, number]> } {
  let d = ''
  const dots: Array<[number, number]> = []
  let run: Array<[number, number]> = []
  const flush = () => {
    if (run.length >= 2) d += linePath(run)
    else if (run.length === 1) dots.push(run[0]!)
    run = []
  }
  for (const p of pts) {
    if (p) run.push(p)
    else flush()
  }
  flush()
  return { d, dots }
}

function lastValue(points: readonly VaultPoint[], key: VaultMetricKey): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const v = points[i]![key]
    if (v !== null) return v
  }
  return null
}

function valueOn(points: readonly VaultPoint[], key: VaultMetricKey, day: number): number | null {
  for (const p of points) if (p.day === day) return p[key]
  return null
}

const Panel = memo(function Panel({ spec, lines }: { spec: MetricSpec; lines: readonly VaultLine[] }) {
  const { tip, show, hide } = useChartTip()
  const [boxW, setBoxW] = useState(W)
  const [hoverDay, setHoverDay] = useState<number | null>(null)
  const shown = lines.filter((l) => spec.perNote || !l.reference)
  const daySet = new Set<number>()
  let dataMax = 0
  for (const l of shown) {
    for (const p of l.points) {
      const v = p[spec.key]
      if (v === null) continue
      daySet.add(p.day)
      if (v > dataMax) dataMax = v
    }
  }
  const days = [...daySet].sort((a, b) => a - b)
  const primary = shown.find((l) => !l.reference) ?? shown[0]
  const latest = primary ? lastValue(primary.points, spec.key) : null
  const head = (
    <div className="chart-sub" style={{ display: 'flex', justifyContent: 'space-between', gap: 6, margin: '6px 0 2px' }}>
      <span>{spec.title}</span>
      <span className="tnum" style={{ color: INK.primary }}>
        {latest === null ? '—' : spec.fmt(latest)}
      </span>
    </div>
  )
  if (days.length === 0) {
    return (
      <div className="chart" style={{ minWidth: 0 }}>
        {head}
        <div className="muted small" style={{ height: H - 12, paddingTop: 8 }}>
          {spec.none}
        </div>
      </div>
    )
  }
  const d0 = days[0]!
  const d1 = days[days.length - 1]!
  const plotW = W - PAD_L - PAD_R
  const plotH = H - TOP - BOTTOM
  let max = spec.max ?? 0
  if (spec.max === undefined) {
    const ticks = niceTicks(dataMax, 2)
    max = ticks[ticks.length - 1]!
  }
  if (!(max > 0)) max = 1
  const xOf = (d: number) => (d1 > d0 ? PAD_L + ((d - d0) / (d1 - d0)) * plotW : PAD_L + plotW / 2)
  const yOf = (v: number) => TOP + plotH - (Math.min(max, Math.max(0, v)) / max) * plotH
  const onMove = (e: ReactPointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (!(rect.width > 0)) return
    const vx = ((e.clientX - rect.left) / rect.width) * W
    let best = d0
    for (const d of days) if (Math.abs(xOf(d) - vx) < Math.abs(xOf(best) - vx)) best = d
    const rows: TipRow[] = shown.map((l) => {
      const v = valueOn(l.points, spec.key, best)
      return { label: l.label, value: v === null ? '—' : spec.fmt(v), color: l.reference ? INK.muted : l.color }
    })
    setBoxW(rect.width)
    setHoverDay(best)
    show((xOf(best) / W) * rect.width, (TOP / H) * rect.height, `${spec.title}, day ${best}`, rows)
  }
  const onLeave = () => {
    setHoverDay(null)
    hide()
  }
  return (
    <div className="chart" style={{ minWidth: 0 }}>
      {head}
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={`${spec.title} by day`}>
        <line x1={PAD_L} x2={W - PAD_R} y1={TOP} y2={TOP} stroke={INK.grid} strokeWidth="1" />
        <text x={PAD_L} y={TOP - 3} fill={INK.muted} fontSize="9" className="tnum">
          {spec.fmt(max)}
        </text>
        <line x1={PAD_L} x2={W - PAD_R} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
        <text x={PAD_L} y={H - 3} fill={INK.muted} fontSize="9" className="tnum">
          {`day ${d0}`}
        </text>
        {d1 > d0 ? (
          <text x={W - PAD_R} y={H - 3} fill={INK.muted} fontSize="9" textAnchor="end" className="tnum">
            {`day ${d1}`}
          </text>
        ) : null}
        {hoverDay !== null ? <line x1={xOf(hoverDay)} x2={xOf(hoverDay)} y1={TOP} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" /> : null}
        {shown.map((l) => {
          const pts = l.points.map((p): [number, number] | null => {
            const v = p[spec.key]
            return v === null ? null : [xOf(p.day), yOf(v)]
          })
          const { d, dots } = segments(pts)
          const color = l.reference ? INK.muted : l.color
          const last = [...pts].reverse().find((p) => p !== null) ?? null
          return (
            <g key={l.key}>
              {d ? (
                <path
                  d={d}
                  fill="none"
                  stroke={color}
                  strokeWidth={l.reference ? 1.5 : 2}
                  strokeDasharray={l.reference ? '3 3' : undefined}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              ) : null}
              {dots.map(([x, y], i) => (
                <circle key={i} cx={x} cy={y} r={l.reference ? 1.5 : 2} fill={color} />
              ))}
              {last && !l.reference ? <circle cx={last[0]} cy={last[1]} r="3" fill={color} stroke={INK.surface} strokeWidth="1.5" /> : null}
            </g>
          )
        })}
        <rect x={PAD_L} y={TOP} width={plotW} height={plotH} fill="transparent" onPointerMove={onMove} onPointerLeave={onLeave} />
      </svg>
      <ChartTip tip={tip} width={boxW} />
    </div>
  )
})

const LegendKey = memo(function LegendKey({ line }: { line: VaultLine }) {
  if (!line.reference) return <span className="legend-line" style={{ background: line.color }} />
  return (
    <svg width="14" height="4" viewBox="0 0 14 4" aria-hidden="true">
      <line x1="0" x2="14" y1="2" y2="2" stroke={INK.muted} strokeWidth="1.5" strokeDasharray="3 3" />
    </svg>
  )
})

/**
 * Vault growth and structure by day (MEMORY_EVOLUTION §5.10): small multiples of notes, bytes, links per note,
 * orphan rate, index coverage and mean note length. A reference line shows only on the per-note panels.
 */
export const VaultChart = memo(function VaultChart({ lines }: { lines: readonly VaultLine[] }) {
  if (!lines.some((l) => l.points.length > 0)) return <div className="muted small">No vault figures yet.</div>
  return (
    <div>
      <div className="chart-legend">
        {lines.map((l) => (
          <span className="legend-item" key={l.key}>
            <LegendKey line={l} />
            {l.reference ? `${l.label} (per-note panels)` : l.label}
          </span>
        ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '2px 14px' }}>
        {METRICS.map((spec) => (
          <Panel key={spec.key} spec={spec} lines={lines} />
        ))}
      </div>
    </div>
  )
})
