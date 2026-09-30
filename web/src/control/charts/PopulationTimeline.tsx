import { memo, useMemo, useState } from 'react'
import { INK, ordinalColor } from './chartTheme'
import { ChartTip, type TipRow, useChartTip } from './ChartTip'

export interface PopPoint {
  tick: number
  byGen: number[]
}

interface Props {
  points: PopPoint[]
  generations: number
  ticksPerDay: number
  renderTick: number
  populationCap: number
}

const W = 360
const H = 130
const LEFT = 26
const TOP = 8
const BOTTOM = 20

/** Population over ticks, stacked by generation (ordinal blue ramp); day boundaries as hairlines. */
export const PopulationTimeline = memo(function PopulationTimeline({ points, generations, ticksPerDay, renderTick, populationCap }: Props) {
  const { tip, show, hide } = useChartTip()
  const [hoverX, setHoverX] = useState<number | null>(null)
  const plotW = W - LEFT - 10
  const plotH = H - TOP - BOTTOM
  const t0 = points.length ? points[0]!.tick : 0
  const t1 = points.length ? points[points.length - 1]!.tick : 1
  const span = Math.max(1, t1 - t0)
  const maxPop = Math.max(populationCap, ...points.map((p) => p.byGen.reduce((a, b) => a + b, 0)))
  const xOf = (t: number) => LEFT + ((t - t0) / span) * plotW
  const yOf = (v: number) => TOP + plotH - (v / maxPop) * plotH

  const areas = useMemo(() => {
    const out: string[] = []
    for (let g = 0; g < generations; g++) {
      let top = ''
      let bottom = ''
      for (let i = 0; i < points.length; i++) {
        const p = points[i]!
        let below = 0
        for (let k = 0; k < g; k++) below += p.byGen[k] ?? 0
        const above = below + (p.byGen[g] ?? 0)
        const x = xOf(p.tick).toFixed(1)
        top += (i === 0 ? 'M' : 'L') + x + ' ' + yOf(above).toFixed(1)
        bottom = 'L' + x + ' ' + yOf(below).toFixed(1) + bottom
      }
      out.push(points.length ? top + bottom + 'Z' : '')
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, generations, maxPop, span, t0])

  const days: number[] = []
  if (ticksPerDay > 0) for (let d = Math.ceil(t0 / ticksPerDay); d * ticksPerDay <= t1; d++) days.push(d)
  const last = points[points.length - 1]
  const lastPop = last ? last.byGen.reduce((a, b) => a + b, 0) : 0

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!points.length) return
    const rect = e.currentTarget.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * W
    const t = t0 + ((px - LEFT) / plotW) * span
    let best = 0
    let bd = Infinity
    for (let i = 0; i < points.length; i++) {
      const d = Math.abs(points[i]!.tick - t)
      if (d < bd) {
        bd = d
        best = i
      }
    }
    const p = points[best]!
    const rows: TipRow[] = []
    for (let g = generations - 1; g >= 0; g--) if ((p.byGen[g] ?? 0) > 0) rows.push({ label: `generation ${g}`, value: String(p.byGen[g]), color: ordinalColor(g) })
    rows.push({ label: 'population', value: String(p.byGen.reduce((a, b) => a + b, 0)) })
    setHoverX(xOf(p.tick))
    show((xOf(p.tick) / W) * rect.width, (TOP / H) * rect.height, `tick ${p.tick}`, rows)
  }

  return (
    <div className="chart">
      <div className="chart-legend">
        {Array.from({ length: generations }, (_, g) => (
          <span className="legend-item" key={g}>
            <span className="legend-swatch" style={{ background: ordinalColor(g) }} />g{g}
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        height={H}
        role="img"
        aria-label="population by generation over time"
        onPointerMove={onMove}
        onPointerLeave={() => {
          setHoverX(null)
          hide()
        }}
      >
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={LEFT} x2={W - 10} y1={yOf(f * maxPop)} y2={yOf(f * maxPop)} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT - 5} y={yOf(f * maxPop) + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {Math.round(f * maxPop)}
            </text>
          </g>
        ))}
        {days.map((d) => (
          <g key={d}>
            <line x1={xOf(d * ticksPerDay)} x2={xOf(d * ticksPerDay)} y1={TOP} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
            <text x={xOf(d * ticksPerDay) + 3} y={H - 6} fill={INK.muted} fontSize="9" className="tnum">
              d{d}
            </text>
          </g>
        ))}
        {areas.map((d, g) => (
          <path key={g} d={d} fill={ordinalColor(g)} opacity="0.85" />
        ))}
        {points.length ? <line x1={xOf(renderTick)} x2={xOf(renderTick)} y1={TOP} y2={TOP + plotH} stroke={INK.primary} strokeWidth="1" opacity="0.6" /> : null}
        {hoverX !== null ? <line x1={hoverX} x2={hoverX} y1={TOP} y2={TOP + plotH} stroke={INK.secondary} strokeWidth="1" /> : null}
        <line x1={LEFT} x2={W - 10} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
        {last ? (
          <text x={W - 10} y={yOf(lastPop) - 4} fill={INK.secondary} fontSize="10" textAnchor="end" className="tnum">
            {lastPop} alive
          </text>
        ) : null}
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
