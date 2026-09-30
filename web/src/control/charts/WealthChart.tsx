import { memo } from 'react'
import { barPath, fmtUsd, INK, niceTicks } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export interface WealthRow {
  id: string
  name: string
  tier: string
  color: string
  balance: number
  alive: boolean
}

interface WealthChartProps {
  rows: WealthRow[]
  tiers: Array<{ name: string; color: string }>
  gini: number
  selectedId: string | null
  onSelect: (id: string) => void
}

const W = 360
const ROW = 22
const LEFT = 64
const RIGHT = 52

/** Horizontal bars, one per agent, coloured by tier (the entity colour), legend by tier. */
export const WealthChart = memo(function WealthChart({ rows, tiers, gini, selectedId, onSelect }: WealthChartProps) {
  const { tip, show, hide } = useChartTip()
  const max = Math.max(0.01, ...rows.map((r) => r.balance))
  const ticks = niceTicks(max, 3)
  const scaleMax = ticks[ticks.length - 1]!
  const plotW = W - LEFT - RIGHT
  const H = rows.length * ROW + 26
  return (
    <div className="chart">
      <div className="chart-legend">
        {tiers.map((t) => (
          <span className="legend-item" key={t.name}>
            <span className="legend-swatch" style={{ background: t.color }} />
            {t.name}
          </span>
        ))}
        <span className="legend-item muted">Gini {gini.toFixed(2)}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="wealth distribution">
        {ticks.map((t) => {
          const x = LEFT + (t / scaleMax) * plotW
          return (
            <g key={t}>
              <line x1={x} x2={x} y1={0} y2={H - 22} stroke={INK.grid} strokeWidth="1" />
              <text x={x} y={H - 8} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
                {fmtUsd(t, 2)}
              </text>
            </g>
          )
        })}
        <line x1={LEFT} x2={LEFT} y1={0} y2={H - 22} stroke={INK.axis} strokeWidth="1" />
        {rows.map((r, i) => {
          const y = i * ROW + 3
          const w = (r.balance / scaleMax) * plotW
          const sel = r.id === selectedId
          return (
            <g
              key={r.id}
              className="bar-row"
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((LEFT + w) / W) * rect.width, (y / H) * rect.height, r.name, [
                  { label: 'balance', value: fmtUsd(r.balance), color: r.color },
                  { label: 'tier', value: r.tier },
                ])
              }}
              onPointerLeave={hide}
              onClick={() => onSelect(r.id)}
              style={{ cursor: 'pointer' }}
            >
              <rect x={0} y={y - 2} width={W} height={ROW} fill={sel ? 'rgba(255,255,255,0.05)' : 'transparent'} />
              <text x={LEFT - 8} y={y + 13} fill={sel ? INK.primary : INK.secondary} fontSize="11" textAnchor="end" opacity={r.alive ? 1 : 0.5}>
                {r.name}
              </text>
              <path d={barPath(LEFT, y, Math.max(2, w), 16)} fill={r.color} opacity={r.alive ? 1 : 0.35} />
              <text x={LEFT + Math.max(2, w) + 6} y={y + 12} fill={INK.secondary} fontSize="10" className="tnum">
                {fmtUsd(r.balance)}
              </text>
            </g>
          )
        })}
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
