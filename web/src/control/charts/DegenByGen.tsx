import { memo } from 'react'
import { columnPath, INK, niceTicks, ordinalColor } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

interface Props {
  counts: number[]
  total: number
}

const W = 360
const H = 120
const LEFT = 30
const TOP = 8
const BOTTOM = 24

/** Degeneration events per generation: ordered categories on the ordinal blue ramp. */
export const DegenByGen = memo(function DegenByGen({ counts, total }: Props) {
  const { tip, show, hide } = useChartTip()
  const max = Math.max(1, ...counts)
  const ticks = niceTicks(max, 3)
  const scaleMax = ticks[ticks.length - 1]!
  const plotW = W - LEFT - 8
  const plotH = H - TOP - BOTTOM
  const slot = plotW / Math.max(1, counts.length)
  const barW = Math.min(24, slot - 6)
  return (
    <div className="chart">
      <div className="chart-sub">{total} events</div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="degeneration events per generation">
        {ticks.map((t) => {
          const y = TOP + plotH - (t / scaleMax) * plotH
          return (
            <g key={t}>
              <line x1={LEFT} x2={W - 8} y1={y} y2={y} stroke={INK.grid} strokeWidth="1" />
              <text x={LEFT - 6} y={y + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
                {t}
              </text>
            </g>
          )
        })}
        {counts.map((v, g) => {
          const x = LEFT + g * slot + (slot - barW) / 2
          const h = (v / scaleMax) * plotH
          return (
            <g
              key={g}
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((x + barW / 2) / W) * rect.width, ((TOP + plotH - h) / H) * rect.height, `generation ${g}`, [{ label: 'degeneration events', value: String(v), color: ordinalColor(g) }])
              }}
              onPointerLeave={hide}
            >
              <rect x={LEFT + g * slot} y={TOP} width={slot} height={plotH} fill="transparent" />
              <path d={columnPath(x, TOP + plotH - h, barW, h)} fill={ordinalColor(g)} />
              {v > 0 ? (
                <text x={x + barW / 2} y={TOP + plotH - h - 4} fill={INK.secondary} fontSize="10" textAnchor="middle" className="tnum">
                  {v}
                </text>
              ) : null}
              <text x={x + barW / 2} y={H - 8} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
                g{g}
              </text>
            </g>
          )
        })}
        <line x1={LEFT} x2={W - 8} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
