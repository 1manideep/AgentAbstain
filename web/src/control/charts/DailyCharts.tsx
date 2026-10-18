import { memo, useMemo, useState } from 'react'
import type { DayRow } from '../../protocol'
import { columnPath, fmtCompact, fmtNum, fmtPct, INK, linePath, niceTicks, ordinalColor, SERIES } from './chartTheme'
import { ChartTip, type TipRow, useChartTip } from './ChartTip'

/*
 * Per-day charts from the `day` rows (DESIGN §16 "daily charts on the day message", §17 day-row fields). Every chart
 * shares one day axis, so the small multiples line up. Day rows come from servers of different ages, so every field
 * is optional here: a missing or non-finite value is a gap, never a NaN in a path.
 */

const W = 360
const LEFT = 30
const RIGHT = 14
const TOP = 8
const BOTTOM = 20
const PLOT_W = W - LEFT - RIGHT
const LINE_H = 104
const COLUMN_H = 120
/** Surface gap between stacked segments (dataviz: 2px). */
const GAP = 2

/** Birth kinds in a fixed colour order (void/agents/lifecycle.py); anything else folds into "other". */
const BIRTH_KINDS = ['fresh', 'offspring', 'replacement', 'arrival'] as const
/** Note channels in a fixed colour order (void/memory/notes.py CHANNELS); anything else folds into "other". */
const CHANNELS = ['observed', 'gossip', 'inherited', 'chronicle', 'tracer', 'operator'] as const
const OTHER = 'other'
const ADOPTERS_COLOR = SERIES[7]

const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const r1 = (v: number): number => Math.round(v * 10) / 10

/** The finite, positive counts of a `{key: n}` record; anything else (missing, null, strings, NaN) is dropped. */
function counts(v: unknown): Map<string, number> {
  const out = new Map<string, number>()
  if (!v || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
    const n = fin(x)
    if (n !== null && n > 0) out.set(k, n)
  }
  return out
}

/** Counts with keys outside `known` summed into "other". */
function folded(v: unknown, known: readonly string[]): Map<string, number> {
  const out = new Map<string, number>()
  for (const [k, n] of counts(v)) {
    const key = known.includes(k) ? k : OTHER
    out.set(key, (out.get(key) ?? 0) + n)
  }
  return out
}

/** Keys present in any map, in the fixed order first, then the rest by name. */
function orderedKeys(maps: ReadonlyArray<ReadonlyMap<string, number>>, fixed: readonly string[]): string[] {
  const seen = new Set<string>()
  for (const m of maps) for (const k of m.keys()) seen.add(k)
  return [...fixed.filter((k) => seen.has(k)), ...[...seen].filter((k) => !fixed.includes(k)).sort()]
}

const fixedColor = (fixed: readonly string[], key: string): string => {
  const i = fixed.indexOf(key)
  return SERIES[i >= 0 ? i : fixed.length]!
}

const sum = (m: ReadonlyMap<string, number>): number => {
  let s = 0
  for (const n of m.values()) s += n
  return s
}

// ---------------------------------------------------------------- shared day axis

interface DayAxis {
  d0: number
  d1: number
  band: number
  xOf: (day: number) => number
  ticks: number[]
}

function dayAxis(days: readonly number[]): DayAxis {
  const d0 = days.length ? days[0]! : 0
  const d1 = days.length ? days[days.length - 1]! : 0
  const n = Math.max(1, d1 - d0 + 1)
  const band = PLOT_W / n
  const step = Math.max(1, Math.ceil(n / 6))
  const ticks: number[] = []
  for (let d = d0; d <= d1; d += step) ticks.push(d)
  return { d0, d1, band, xOf: (day) => LEFT + (day - d0 + 0.5) * band, ticks }
}

/** Index of the day nearest a pointer x (in viewBox units). */
function nearestDay(axis: DayAxis, days: readonly number[], px: number): number {
  const target = axis.d0 + (px - LEFT) / axis.band - 0.5
  let best = 0
  let bd = Infinity
  for (let i = 0; i < days.length; i++) {
    const d = Math.abs(days[i]! - target)
    if (d < bd) {
      bd = d
      best = i
    }
  }
  return best
}

const DayLabels = memo(function DayLabels({ axis, y }: { axis: DayAxis; y: number }) {
  return (
    <>
      {axis.ticks.map((d) => (
        <text key={d} x={r1(axis.xOf(d))} y={y} fill={INK.muted} fontSize="9" textAnchor="middle" className="tnum">
          d{d}
        </text>
      ))}
    </>
  )
})

// ---------------------------------------------------------------- lines over days

export interface DayLineSeries {
  key: string
  label: string
  color: string
  /** One value per day of `days`; null is a gap. */
  values: Array<number | null>
}

interface DayLinesProps {
  days: number[]
  series: DayLineSeries[]
  /** Fixed top of the y scale (rates: 1); otherwise a nice step above the largest value. */
  yMax?: number
  /** Counts: whole-number ticks only. */
  integer?: boolean
  format: (v: number) => string
  label: string
  empty: string
}

/** Runs of consecutive finite values as point lists. */
function runs(values: ReadonlyArray<number | null>, xs: readonly number[], yOf: (v: number) => number): Array<Array<[number, number]>> {
  const out: Array<Array<[number, number]>> = []
  let cur: Array<[number, number]> = []
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    if (v === null || v === undefined || !Number.isFinite(v)) {
      if (cur.length) out.push(cur)
      cur = []
      continue
    }
    cur.push([xs[i]!, yOf(v)])
  }
  if (cur.length) out.push(cur)
  return out
}

/** One or more series over the day axis: 2px lines broken at missing days, lone points as dots, crosshair tooltip. */
export const DayLines = memo(function DayLines({ days, series, yMax, integer = false, format, label, empty }: DayLinesProps) {
  const { tip, show, hide } = useChartTip()
  const [hover, setHover] = useState<number | null>(null)
  const axis = useMemo(() => dayAxis(days), [days])
  const plotH = LINE_H - TOP - BOTTOM
  const { yTicks, scaleMax, hasData } = useMemo(() => {
    let max = 0
    let any = false
    for (const s of series)
      for (const v of s.values)
        if (v !== null && Number.isFinite(v)) {
          any = true
          if (v > max) max = v
        }
    if (yMax !== undefined && yMax > 0) return { yTicks: [0, yMax / 2, yMax], scaleMax: yMax, hasData: any }
    const t = max > 0 ? niceTicks(max, integer ? Math.max(1, Math.min(3, Math.ceil(max))) : 3) : [0]
    const top = t[t.length - 1]!
    return { yTicks: t, scaleMax: top > 0 ? top : 1, hasData: any }
  }, [series, yMax, integer])
  if (!hasData) return <div className="muted small">{empty}</div>
  // values beyond the scale (a rate above 1) are clamped to the frame
  const yOf = (v: number) => r1(TOP + plotH - (Math.max(0, Math.min(v, scaleMax)) / scaleMax) * plotH)
  const xs = days.map((d) => r1(axis.xOf(d)))
  const paths = series.map((s) => ({ s, runs: runs(s.values, xs, yOf) }))
  const single = series.length === 1 ? paths[0] : undefined
  const lastRun = single?.runs[single.runs.length - 1]
  const lastPoint = lastRun?.[lastRun.length - 1]
  const lastValue = single ? [...single.s.values].reverse().find((v): v is number => v !== null && Number.isFinite(v)) : undefined

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    if (!(rect.width > 0)) return
    const i = nearestDay(axis, days, ((e.clientX - rect.left) / rect.width) * W)
    const rows: TipRow[] = series.map((s) => {
      const v = s.values[i]
      return { label: s.label, value: v !== null && v !== undefined && Number.isFinite(v) ? format(v) : 'no data', color: s.color }
    })
    setHover(i)
    show((xs[i]! / W) * rect.width, (TOP / LINE_H) * rect.height, `day ${days[i]}`, rows)
  }

  return (
    <div className="chart">
      {series.length > 1 ? (
        <div className="chart-legend">
          {series.map((s) => (
            <span className="legend-item" key={s.key}>
              <span className="legend-line" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      ) : null}
      <svg
        viewBox={`0 0 ${W} ${LINE_H}`}
        width="100%"
        height={LINE_H}
        role="img"
        aria-label={label}
        onPointerMove={onMove}
        onPointerLeave={() => {
          setHover(null)
          hide()
        }}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={LEFT} x2={W - RIGHT} y1={yOf(t)} y2={yOf(t)} stroke={t === 0 ? INK.axis : INK.grid} strokeWidth="1" />
            <text x={LEFT - 5} y={yOf(t) + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {format(t)}
            </text>
          </g>
        ))}
        <DayLabels axis={axis} y={LINE_H - 6} />
        {hover !== null && xs[hover] !== undefined ? <line x1={xs[hover]} x2={xs[hover]} y1={TOP} y2={TOP + plotH} stroke={INK.secondary} strokeWidth="1" /> : null}
        {paths.map(({ s, runs: rs }) => (
          <g key={s.key}>
            {rs.map((pts, k) =>
              pts.length > 1 ? (
                <path key={k} d={linePath(pts)} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              ) : (
                <circle key={k} cx={pts[0]![0]} cy={pts[0]![1]} r="4" fill={s.color} stroke={INK.surface} strokeWidth="2" />
              ),
            )}
          </g>
        ))}
        {single && lastPoint && lastValue !== undefined ? (
          <>
            <circle cx={lastPoint[0]} cy={lastPoint[1]} r="4" fill={single.s.color} stroke={INK.surface} strokeWidth="2" />
            <text x={Math.min(lastPoint[0] + 6, W - 2)} y={lastPoint[1] - 7} fill={INK.secondary} fontSize="10" textAnchor="end" className="tnum">
              {format(lastValue)}
            </text>
          </>
        ) : null}
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})

// ---------------------------------------------------------------- births up, deaths down

interface Segment {
  key: string
  n: number
  color: string
}

interface BirthDeathProps {
  days: number[]
  births: Array<Map<string, number>>
  deaths: Array<Map<string, number>>
  tierColor: (tier: string) => string
  tierOrder: readonly string[]
}

/** Square at the baseline (top), 4px rounded at the data end (bottom). */
function downColumnPath(x: number, y: number, w: number, h: number, r = 4): string {
  if (h <= 0.01) return ''
  const rr = Math.min(r, w / 2, h)
  return `M${x} ${y}V${r1(y + h - rr)}Q${x} ${r1(y + h)} ${r1(x + rr)} ${r1(y + h)}H${r1(x + w - rr)}Q${r1(x + w)} ${r1(y + h)} ${r1(x + w)} ${r1(y + h - rr)}V${y}Z`
}

/** Births per day stacked by kind above the baseline, deaths stacked by tier below it. */
export const BirthDeathColumns = memo(function BirthDeathColumns({ days, births, deaths, tierColor, tierOrder }: BirthDeathProps) {
  const { tip, show, hide } = useChartTip()
  const axis = useMemo(() => dayAxis(days), [days])
  const kinds = useMemo(() => orderedKeys(births, BIRTH_KINDS), [births])
  const tiers = useMemo(() => orderedKeys(deaths, tierOrder), [deaths, tierOrder])
  const upMax = Math.max(0, ...births.map(sum))
  const downMax = Math.max(0, ...deaths.map(sum))
  if (upMax <= 0 && downMax <= 0) return <div className="muted small">No births or deaths yet.</div>
  const plotH = COLUMN_H - TOP - BOTTOM
  const unit = plotH / (upMax + downMax)
  const baseY = r1(TOP + upMax * unit)
  const bw = r1(Math.max(1, Math.min(24, axis.band - GAP, axis.band * 0.7)))
  const kindColor = (k: string) => fixedColor(BIRTH_KINDS, k)

  const stacks = days.map((day, i) => {
    const x = r1(axis.xOf(day) - bw / 2)
    const up: Segment[] = kinds.filter((k) => (births[i]?.get(k) ?? 0) > 0).map((k) => ({ key: k, n: births[i]!.get(k)!, color: kindColor(k) }))
    const down: Segment[] = tiers.filter((t) => (deaths[i]?.get(t) ?? 0) > 0).map((t) => ({ key: t, n: deaths[i]!.get(t)!, color: tierColor(t) }))
    return { day, i, x, up, down }
  })

  const onEnter = (e: React.PointerEvent<SVGRectElement>, s: (typeof stacks)[number]) => {
    const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
    const rows: TipRow[] = [
      { label: 'births', value: String(sum(births[s.i]!)) },
      ...s.up.map((g) => ({ label: g.key, value: String(g.n), color: g.color })),
      { label: 'deaths', value: String(sum(deaths[s.i]!)) },
      ...s.down.map((g) => ({ label: g.key, value: String(g.n), color: g.color })),
    ]
    show((axis.xOf(s.day) / W) * rect.width, (TOP / COLUMN_H) * rect.height, `day ${s.day}`, rows)
  }

  return (
    <div className="chart">
      <div className="chart-legend">
        {kinds.length ? <span className="legend-item muted">born</span> : null}
        {kinds.map((k) => (
          <span className="legend-item" key={'b' + k}>
            <span className="legend-swatch" style={{ background: kindColor(k) }} />
            {k}
          </span>
        ))}
        {tiers.length ? <span className="legend-item muted">died</span> : null}
        {tiers.map((t) => (
          <span className="legend-item" key={'d' + t}>
            <span className="legend-swatch" style={{ background: tierColor(t) }} />
            {t}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${COLUMN_H}`} width="100%" height={COLUMN_H} role="img" aria-label="births by kind and deaths by tier per day" onPointerLeave={hide}>
        {upMax > 0 ? (
          <g>
            <line x1={LEFT} x2={W - RIGHT} y1={TOP} y2={TOP} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT - 5} y={TOP + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {fmtCompact(upMax)}
            </text>
          </g>
        ) : null}
        {downMax > 0 ? (
          <g>
            <line x1={LEFT} x2={W - RIGHT} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT - 5} y={TOP + plotH + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {fmtCompact(downMax)}
            </text>
          </g>
        ) : null}
        {stacks.map((s) => {
          let y = baseY
          const up = s.up.map((g, k) => {
            const h = g.n * unit
            const top = r1(y - h)
            const bottom = r1(y - (k > 0 ? GAP / 2 : 0))
            const drawTop = k < s.up.length - 1 ? r1(top + GAP / 2) : top
            y -= h
            const hh = r1(bottom - drawTop)
            if (hh <= 0.01) return null
            return k === s.up.length - 1 ? (
              <path key={'u' + g.key} d={columnPath(s.x, drawTop, bw, hh)} fill={g.color} />
            ) : (
              <rect key={'u' + g.key} x={s.x} y={drawTop} width={bw} height={hh} fill={g.color} />
            )
          })
          y = baseY
          const down = s.down.map((g, k) => {
            const h = g.n * unit
            const top = r1(y + (k > 0 ? GAP / 2 : 0))
            const end = r1(y + h)
            const drawEnd = k < s.down.length - 1 ? r1(end - GAP / 2) : end
            y += h
            const hh = r1(drawEnd - top)
            if (hh <= 0.01) return null
            return k === s.down.length - 1 ? (
              <path key={'d' + g.key} d={downColumnPath(s.x, top, bw, hh)} fill={g.color} />
            ) : (
              <rect key={'d' + g.key} x={s.x} y={top} width={bw} height={hh} fill={g.color} />
            )
          })
          return (
            <g key={s.day}>
              {up}
              {down}
              <rect
                x={r1(axis.xOf(s.day) - axis.band / 2)}
                y={TOP}
                width={r1(axis.band)}
                height={plotH}
                fill="transparent"
                onPointerEnter={(e) => onEnter(e, s)}
              />
            </g>
          )
        })}
        <line x1={LEFT} x2={W - RIGHT} y1={baseY} y2={baseY} stroke={INK.axis} strokeWidth="1" />
        <DayLabels axis={axis} y={COLUMN_H - 6} />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})

// ---------------------------------------------------------------- the card body

interface DailyChartsProps {
  /** Day rows sorted by day, one per day. */
  rows: DayRow[]
  tierColor: (tier: string) => string
  /** Tier names in display order (down the capability ladder); tiers not listed follow by name. */
  tierOrder?: readonly string[]
}

const NO_TIERS: readonly string[] = []

/** Values of one series per day, null where the row lacks it. */
const perDay = (rows: readonly DayRow[], pick: (r: DayRow) => unknown): Array<number | null> => rows.map((r) => fin(pick(r)))

/** Line series from a `{key: n}` field, folded onto a fixed key list; days without the field are gaps. */
function keyedSeries(rows: readonly DayRow[], field: (r: DayRow) => unknown, fixed: readonly string[], dropAllZero: boolean): DayLineSeries[] {
  const present = rows.map((r) => {
    const v = field(r)
    return v && typeof v === 'object' && !Array.isArray(v)
  })
  const maps = rows.map((r) => folded(field(r), fixed))
  const keys = orderedKeys(maps, fixed)
  // a key missing from a row that has the field reads as 0 (tracer_reach_by_channel omits empty channels)
  return keys
    .map((k) => ({ key: k, label: k, color: fixedColor(fixed, k), values: maps.map((m, i) => (present[i] ? (m.get(k) ?? 0) : null)) }))
    .filter((s) => !dropAllZero || s.values.some((v) => v !== null && v > 0))
}

/** Hops drawn as a line each; deeper hops share one line. */
const MAX_HOP_LINES = 4

/** One row's mean similarity per hop number; non-numeric hops and missing means are dropped. */
function hopMeans(v: unknown): Map<number, number> {
  const out = new Map<number, number>()
  if (!v || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
    const n = fin(x)
    if (n !== null && /^\d+$/.test(k)) out.set(Number(k), n)
  }
  return out
}

/**
 * Gossip similarity per hop. Hops 1-4 take every other step of the ordinal ramp so neighbours stay apart; hops past
 * the fourth (rare: chains are short) share one "hop 5+" line, the unweighted mean of their per-hop means.
 */
function gossipSeries(rows: readonly DayRow[]): DayLineSeries[] {
  const perRow = rows.map((r) => hopMeans(r.gossip_similarity_by_hop))
  const hops = new Set<number>()
  for (const m of perRow) for (const h of m.keys()) hops.add(Math.min(h, MAX_HOP_LINES + 1))
  return [...hops]
    .sort((a, b) => a - b)
    .map((h) => {
      const deep = h > MAX_HOP_LINES
      return {
        key: String(h),
        label: deep ? `hop ${h}+` : `hop ${h}`,
        color: ordinalColor(Math.max(0, h - 1) * 2),
        values: perRow.map((m) => {
          if (!deep) return m.get(h) ?? null
          let total = 0
          let n = 0
          for (const [k, v] of m)
            if (k > MAX_HOP_LINES) {
              total += v
              n++
            }
          return n ? total / n : null
        }),
      }
    })
}

const fmtSimilarity = (v: number) => fmtNum(v, 2)
const fmtRate = (v: number) => fmtPct(v)

/** The daily card's charts: births and deaths, gossip similarity by hop, notes by channel, gadget failures, tracer. */
export const DailyCharts = memo(function DailyCharts({ rows, tierColor, tierOrder = NO_TIERS }: DailyChartsProps) {
  const data = useMemo(() => {
    const days = rows.map((r) => r.day)
    const births = rows.map((r) => {
      const m = folded(r.births_by_kind, BIRTH_KINDS)
      const n = fin(r.births)
      // older rows (or rows from a filtered export) carry only the total
      if (m.size === 0 && n !== null && n > 0) m.set(OTHER, n)
      return m
    })
    const deaths = rows.map((r) => {
      const m = counts(r.deaths_by_tier)
      const n = fin(r.deaths)
      const known = sum(m)
      if (n !== null && n > known) m.set('unknown tier', n - known)
      return m
    })
    const gossip = gossipSeries(rows)
    const notes = keyedSeries(rows, (r) => r.notes_by_channel, CHANNELS, true)
    const failure: DayLineSeries[] = [{ key: 'failure', label: 'use-failure rate', color: SERIES[0], values: perDay(rows, (r) => r.gadget_use_failure_rate) }]
    const hasTracer = rows.some((r) => (r.tracer_reach_by_channel && typeof r.tracer_reach_by_channel === 'object') || fin(r.tracer_adopters) !== null || fin(r.tracer_reach) !== null)
    let tracer: DayLineSeries[] = []
    if (hasTracer) {
      tracer = keyedSeries(rows, (r) => r.tracer_reach_by_channel, CHANNELS, false)
      if (tracer.length === 0 && rows.some((r) => fin(r.tracer_reach) !== null))
        tracer.push({ key: 'reach', label: 'reach', color: SERIES[0], values: perDay(rows, (r) => r.tracer_reach) })
      if (rows.some((r) => fin(r.tracer_adopters) !== null))
        tracer.push({ key: 'adopters', label: 'adopters', color: ADOPTERS_COLOR, values: perDay(rows, (r) => r.tracer_adopters) })
    }
    return { days, births, deaths, gossip, notes, failure, hasTracer, tracer }
  }, [rows])

  return (
    <div className="daily-charts">
      <div className="chart-sub">Births by kind and deaths by tier</div>
      <BirthDeathColumns days={data.days} births={data.births} deaths={data.deaths} tierColor={tierColor} tierOrder={tierOrder} />
      <div className="chart-sub">Gossip similarity by hop (mean)</div>
      <DayLines days={data.days} series={data.gossip} yMax={1} format={fmtSimilarity} label="gossip similarity by hop per day" empty="No gossip yet." />
      <div className="chart-sub">Live notes by channel</div>
      <DayLines days={data.days} series={data.notes} integer format={fmtCompact} label="live notes by channel per day" empty="No notes yet." />
      <div className="chart-sub">Gadget use-failure rate</div>
      <DayLines days={data.days} series={data.failure} yMax={1} format={fmtRate} label="gadget use-failure rate per day" empty="No gadget uses yet." />
      {data.hasTracer ? (
        <>
          <div className="chart-sub">Tracer reach by channel and adopters</div>
          <DayLines days={data.days} series={data.tracer} integer format={fmtCompact} label="tracer reach by channel and adopters per day" empty="No tracer holders yet." />
        </>
      ) : null}
    </div>
  )
})
