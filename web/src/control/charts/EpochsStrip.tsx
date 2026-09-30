import { memo } from 'react'
import { EPOCH_COLORS, INK } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export interface EpochBlock {
  key: string
  kind: string
  startDay: number
  endDay: number
  state: 'past' | 'active' | 'scheduled'
  detail: string
}

interface Props {
  blocks: EpochBlock[]
  currentDay: number
  maxDay: number
}

const W = 360
const H = 58
const LEFT = 8
const TOP = 8

/** Epochs on a day axis: past, active and scheduled blocks coloured by kind. */
export const EpochsStrip = memo(function EpochsStrip({ blocks, currentDay, maxDay }: Props) {
  const { tip, show, hide } = useChartTip()
  const span = Math.max(1, maxDay)
  const plotW = W - LEFT - 8
  const xOf = (d: number) => LEFT + (Math.min(span, Math.max(0, d)) / span) * plotW
  const kinds = Array.from(new Set(blocks.map((b) => b.kind)))
  return (
    <div className="chart">
      <div className="chart-legend">
        {kinds.length === 0 ? <span className="muted small">No epochs applied or scheduled.</span> : null}
        {kinds.map((k) => (
          <span className="legend-item" key={k}>
            <span className="legend-swatch" style={{ background: EPOCH_COLORS[k] ?? INK.secondary }} />
            {k}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="epochs">
        <line x1={LEFT} x2={W - 8} y1={TOP + 16} y2={TOP + 16} stroke={INK.axis} strokeWidth="1" />
        {Array.from({ length: span + 1 }, (_, d) => (
          <text key={d} x={xOf(d)} y={H - 6} fill={INK.muted} fontSize="9" textAnchor="middle" className="tnum">
            {d}
          </text>
        ))}
        {blocks.map((b) => {
          const x0 = xOf(b.startDay)
          const x1 = Math.max(x0 + 3, xOf(b.endDay))
          const color = EPOCH_COLORS[b.kind] ?? INK.secondary
          return (
            <rect
              key={b.key}
              x={x0}
              y={TOP + 4}
              width={x1 - x0 - 2}
              height={24}
              rx={4}
              fill={color}
              opacity={b.state === 'scheduled' ? 0.35 : b.state === 'past' ? 0.6 : 1}
              stroke={b.state === 'scheduled' ? color : 'none'}
              strokeDasharray={b.state === 'scheduled' ? '3 3' : undefined}
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((x0 + x1) / 2 / W) * rect.width, (TOP / H) * rect.height, `${b.kind} · ${b.state}`, [
                  { label: `day ${b.startDay} → ${b.endDay}`, value: b.detail, color },
                ])
              }}
              onPointerLeave={hide}
            />
          )
        })}
        <line x1={xOf(currentDay)} x2={xOf(currentDay)} y1={TOP} y2={TOP + 32} stroke={INK.primary} strokeWidth="1.5" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
