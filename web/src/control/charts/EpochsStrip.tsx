import { memo } from 'react'
import type { Epoch } from '../../protocol'
import type { EpochSpan } from '../../state/series'
import { EPOCH_COLORS, INK } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export type EpochBlockState = 'past' | 'active' | 'scheduled'

export interface EpochBlock {
  key: string
  /** Server epoch id; null for an active or scheduled epoch sent without one. */
  id: string | null
  kind: string
  startDay: number
  /** The day the epoch expires: the block covers startDay .. endDay - 1. */
  endDay: number
  state: EpochBlockState
  detail: string
  /** Row of the strip, so overlapping epochs do not hide each other. */
  lane: number
}

/** Rows the strip grows to before overlapping epochs share the last one. */
export const MAX_EPOCH_LANES = 3

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function leverDetail(e: { kind: string; scarcity?: number | null; weather_baseline?: number | null; arrival_agent_id?: string | null }): string[] {
  const parts: string[] = []
  if (e.scarcity != null) parts.push(`scarcity ${e.scarcity}`)
  if (e.weather_baseline != null) parts.push(`weather ${e.weather_baseline}`)
  if (e.arrival_agent_id) parts.push(`arrival ${e.arrival_agent_id}`)
  return parts
}

function spanBlock(s: EpochSpan): EpochBlock {
  const parts = leverDetail(s)
  if (s.source === 'ended') parts.push('start not seen')
  if (s.ended_day !== null && s.ends_day !== null && s.ended_day < s.ends_day) parts.push(`ended day ${s.ended_day}`)
  const endDay = Math.max(s.day, s.ends_day ?? s.ended_day ?? s.day + 1)
  return {
    key: s.id,
    id: s.id,
    kind: s.kind,
    startDay: s.day,
    endDay,
    // active until the matching ended event; the series also retires a span whose ends_day a snapshot has passed
    // (its ended event may fall outside the backlog)
    state: s.ended_day !== null || s.state === 'past' ? 'past' : 'active',
    detail: parts.join(' · ') || s.kind,
    lane: 0,
  }
}

function epochBlock(e: Epoch, state: EpochBlockState, key: string): EpochBlock {
  const day = num(e.day) ?? 0
  const endDay = Math.max(day, num(e.ends_day) ?? day + (num(e.duration_days) ?? 1))
  return { key, id: e.id ?? null, kind: String(e.kind), startDay: day, endDay, state, detail: leverDetail({ ...e, kind: String(e.kind) }).join(' · ') || String(e.kind), lane: 0 }
}

const STATE_ORDER: Record<EpochBlockState, number> = { past: 0, active: 1, scheduled: 2 }

/** Greedy interval packing: each block takes the first row whose last block has ended by its start. */
function assignLanes(blocks: EpochBlock[], maxLanes: number): void {
  const laneEnds: number[] = []
  for (const b of blocks) {
    let lane = laneEnds.findIndex((end) => end <= b.startDay)
    if (lane < 0) lane = laneEnds.length < maxLanes ? laneEnds.length : maxLanes - 1
    b.lane = lane
    laneEnds[lane] = Math.max(laneEnds[lane] ?? 0, b.endDay)
  }
}

/**
 * One block per epoch id: started epochs (seen as events, or as the snapshot's active epoch when the page loaded
 * mid-epoch) from their first day to ends_day, active until their ended event; then the snapshot's active epoch if
 * the series has no span for it, and the scheduled epochs not yet started. Sorted by first day, packed into lanes.
 */
export function epochBlocks(spans: readonly EpochSpan[], epochs: { active: Epoch | null; scheduled: readonly Epoch[] }, maxLanes = MAX_EPOCH_LANES): EpochBlock[] {
  const out = spans.map(spanBlock)
  const ids = new Set(out.map((b) => b.id))
  const a = epochs.active
  if (a && !(a.id ? ids.has(a.id) : out.some((b) => b.kind === a.kind && b.startDay === a.day))) {
    out.push(epochBlock(a, 'active', a.id ?? `active-${a.kind}-${a.day}`))
    if (a.id) ids.add(a.id)
  }
  for (const s of epochs.scheduled) {
    if (s.id && ids.has(s.id)) continue
    out.push(epochBlock(s, 'scheduled', `sched-${s.id ?? `${s.kind}-${s.day}`}`))
  }
  out.sort((x, y) => x.startDay - y.startDay || STATE_ORDER[x.state] - STATE_ORDER[y.state] || (x.key < y.key ? -1 : x.key > y.key ? 1 : 0))
  assignLanes(out, Math.max(1, maxLanes))
  return out
}

/** Day-axis label step so the axis carries at most about a dozen labels. */
export function dayStep(span: number): number {
  for (const step of [1, 2, 5, 10, 20, 50, 100, 200, 500]) if (span / step <= 12) return step
  return 1000
}

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
