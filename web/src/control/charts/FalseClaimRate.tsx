import { memo } from 'react'
import { barPath, fmtPct, INK } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export interface ClaimRow {
  tier: string
  color: string
  made: number
  falsy: number
}

const W = 360
const ROW = 24
const LEFT = 70

/** False-claim rate per tier: horizontal bars in tier colour, rate labelled at the tip. */
export const FalseClaimRate = memo(function FalseClaimRate({ rows }: { rows: ClaimRow[] }) {
  const { tip, show, hide } = useChartTip()
  if (rows.length === 0) return <div className="muted small">No claims checked yet.</div>
  const H = rows.length * ROW + 22
  const plotW = W - LEFT - 70
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="false claim rate by tier">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={LEFT + f * plotW} x2={LEFT + f * plotW} y1={0} y2={H - 18} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT + f * plotW} y={H - 5} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
              {fmtPct(f)}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const rate = r.made ? r.falsy / r.made : 0
          const y = i * ROW + 3
          const w = rate * plotW
          return (
            <g
              key={r.tier}
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((LEFT + w) / W) * rect.width, (y / H) * rect.height, r.tier, [
                  { label: 'false claims', value: fmtPct(rate, 1), color: r.color },
                  { label: 'claims checked', value: String(r.made) },
                ])
              }}
              onPointerLeave={hide}
            >
              <text x={LEFT - 8} y={y + 13} fill={INK.secondary} fontSize="11" textAnchor="end">
                {r.tier}
              </text>
              <path d={barPath(LEFT, y, Math.max(2, w), 16)} fill={r.color} />
              <text x={LEFT + Math.max(2, w) + 6} y={y + 12} fill={INK.secondary} fontSize="10" className="tnum">
                {fmtPct(rate, 1)} · {r.falsy}/{r.made}
              </text>
            </g>
          )
        })}
        <line x1={LEFT} x2={LEFT} y1={0} y2={H - 18} stroke={INK.axis} strokeWidth="1" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
