import { memo } from 'react'
import { STRESS_BINS } from '../../state/series'
import { fmtNum, fmtPct, INK, linePath } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

export interface TierCurve {
  tier: string
  color: string
  /** mean per stress bin, NaN when empty */
  coherence: number[]
  invalid: number[]
  n: number[]
}

interface Props {
  curves: TierCurve[]
}

const W = 360
const H = 120
const LEFT = 34
const TOP = 10
const BOTTOM = 22

function Panel({ title, curves, pick, fmt, max }: { title: string; curves: TierCurve[]; pick: (c: TierCurve) => number[]; fmt: (v: number) => string; max: number }) {
  const { tip, show, hide } = useChartTip()
  const plotW = W - LEFT - 12
  const plotH = H - TOP - BOTTOM
  const xOf = (b: number) => LEFT + ((b + 0.5) / STRESS_BINS) * plotW
  const yOf = (v: number) => TOP + plotH - (Math.min(max, Math.max(0, v)) / max) * plotH
  return (
    <div className="chart">
      <div className="chart-sub">{title}</div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={title}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={LEFT} x2={W - 12} y1={yOf(f * max)} y2={yOf(f * max)} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT - 6} y={yOf(f * max) + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {fmt(f * max)}
            </text>
          </g>
        ))}
        {Array.from({ length: STRESS_BINS }, (_, b) => (
          <text key={b} x={xOf(b)} y={H - 6} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
            {(b / STRESS_BINS).toFixed(1)}–{((b + 1) / STRESS_BINS).toFixed(1)}
          </text>
        ))}
        {curves.map((c) => {
          const vals = pick(c)
          const pts: Array<[number, number]> = []
          vals.forEach((v, b) => {
            if (Number.isFinite(v)) pts.push([xOf(b), yOf(v)])
          })
          return (
            <g key={c.tier}>
              <path d={linePath(pts)} fill="none" stroke={c.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              {vals.map((v, b) =>
                Number.isFinite(v) ? (
                  <circle
                    key={b}
                    cx={xOf(b)}
                    cy={yOf(v)}
                    r="4"
                    fill={c.color}
                    stroke={INK.surface}
                    strokeWidth="2"
                    onPointerEnter={(e) => {
                      const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                      show((xOf(b) / W) * rect.width, (yOf(v) / H) * rect.height, `stress ${(b / STRESS_BINS).toFixed(1)}–${((b + 1) / STRESS_BINS).toFixed(1)}`, [
                        { label: `${c.tier} (${c.n[b]} rows)`, value: fmt(v), color: c.color },
                      ])
                    }}
                    onPointerLeave={hide}
                  />
                ) : null,
              )}
            </g>
          )
        })}
        <line x1={LEFT} x2={W - 12} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
}

/** Observed degradation (text coherence, invalid-action rate) vs stress bin per tier — the EIML3 comparison chart. */
export const DegradationVsStress = memo(function DegradationVsStress({ curves }: Props) {
  if (curves.length === 0) return <div className="muted small">Waiting for metrics rows.</div>
  return (
    <div>
      <div className="chart-legend">
        {curves.map((c) => (
          <span className="legend-item" key={c.tier}>
            <span className="legend-line" style={{ background: c.color }} />
            {c.tier}
          </span>
        ))}
      </div>
      <Panel title="Text coherence (mean)" curves={curves} pick={(c) => c.coherence} fmt={(v) => fmtNum(v, 2)} max={1} />
      <Panel title="Invalid action rate" curves={curves} pick={(c) => c.invalid} fmt={(v) => fmtPct(v)} max={1} />
    </div>
  )
})
