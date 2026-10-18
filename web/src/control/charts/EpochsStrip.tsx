import { memo } from 'react'
import { EPOCH_COLORS, INK } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'
import { dayStep, type EpochBlock } from './degradationEpochsModel'

/**
 * Compatibility for degradationEpochs.test.tsx, which still imports these from here: the model lives in
 * degradationEpochsModel.ts. Remove once that test imports from the model.
 */
// oxlint-disable-next-line react/only-export-components
export { dayStep, epochBlocks, MAX_EPOCH_LANES } from './degradationEpochsModel'

interface Props {
  blocks: EpochBlock[]
  /** The render clock's day, fractional within the day. */
  currentDay: number
}

const W = 360
const LEFT = 8
const RIGHT = 8
const TOP = 6
const LANE_H = 18
const LANE_GAP = 4
const AXIS_H = 18

/** Epochs on a day axis: past, active and scheduled blocks coloured by kind, one per epoch id. */
export const EpochsStrip = memo(function EpochsStrip({ blocks, currentDay }: Props) {
  const { tip, show, hide } = useChartTip()
  const lanes = Math.max(1, ...blocks.map((b) => b.lane + 1))
  const span = Math.max(1, Math.floor(currentDay) + 2, ...blocks.map((b) => b.endDay))
  const step = dayStep(span)
  const plotW = W - LEFT - RIGHT
  const rowsH = lanes * LANE_H + (lanes - 1) * LANE_GAP
  const axisY = TOP + rowsH + 4
  const H = axisY + AXIS_H
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
        <line x1={LEFT} x2={W - RIGHT} y1={axisY} y2={axisY} stroke={INK.axis} strokeWidth="1" />
        {Array.from({ length: Math.floor(span / step) + 1 }, (_, i) => i * step).map((d) => (
          <g key={d}>
            <line x1={xOf(d)} x2={xOf(d)} y1={axisY} y2={axisY + 3} stroke={INK.axis} strokeWidth="1" />
            <text x={xOf(d)} y={H - 4} fill={INK.muted} fontSize="9" textAnchor="middle" className="tnum">
              {d}
            </text>
          </g>
        ))}
        {blocks.map((b) => {
          const x0 = xOf(b.startDay)
          const x1 = Math.max(x0 + 5, xOf(b.endDay))
          const y = TOP + b.lane * (LANE_H + LANE_GAP)
          const color = EPOCH_COLORS[b.kind] ?? INK.secondary
          const last = b.endDay - 1
          const days = last > b.startDay ? `days ${b.startDay}–${last}` : `day ${b.startDay}`
          return (
            <rect
              key={b.key}
              x={x0}
              y={y}
              width={x1 - x0 - 2}
              height={LANE_H}
              rx={4}
              fill={color}
              fillOpacity={b.state === 'scheduled' ? 0.3 : b.state === 'past' ? 0.5 : 1}
              stroke={b.state === 'scheduled' ? color : 'none'}
              strokeDasharray={b.state === 'scheduled' ? '3 3' : undefined}
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((x0 + x1) / 2 / W) * rect.width, (y / H) * rect.height, `${b.kind} · ${b.state}${b.id ? ` · ${b.id}` : ''}`, [
                  { label: `${days} (ends day ${b.endDay})`, value: b.detail, color },
                ])
              }}
              onPointerLeave={hide}
            />
          )
        })}
        <line x1={xOf(currentDay)} x2={xOf(currentDay)} y1={TOP - 2} y2={axisY} stroke={INK.primary} strokeWidth="1.5" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
