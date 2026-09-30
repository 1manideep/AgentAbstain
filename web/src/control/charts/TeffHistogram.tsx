import { memo, useMemo } from 'react'
import { TEFF_BINS, TEFF_MAX, TEFF_MIN } from '../../state/series'
import { columnPath, fmtNum, INK, niceTicks, SERIES } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

interface TeffHistogramProps {
  bins: Int32Array
  samples: number
  /** Collapse temperature per tier, when known from degeneration events. */
  collapse: Array<{ tier: string; tC: number; color: string }>
  label: string
}

const W = 360
const H = 150
const LEFT = 30
const BOTTOM = 26
const TOP = 8

/** Column histogram of T_eff over the last 240 ticks, one hue; collapse temperatures as hairlines. */
export const TeffHistogram = memo(function TeffHistogram({ bins, samples, collapse, label }: TeffHistogramProps) {
  const { tip, show, hide } = useChartTip()
  const data = useMemo(() => Array.from(bins), [bins])
  const max = Math.max(1, ...data)
  const ticks = niceTicks(max, 3)
  const scaleMax = ticks[ticks.length - 1]!
  const plotW = W - LEFT - 8
  const plotH = H - TOP - BOTTOM
  const slot = plotW / TEFF_BINS
  const barW = Math.min(24, slot - 2)
  const xOf = (t: number) => LEFT + ((t - TEFF_MIN) / (TEFF_MAX - TEFF_MIN)) * plotW
  return (
    <div className="chart">
      <div className="chart-sub">
        {label} · {samples} samples
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={label}>
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
        {data.map((v, i) => {
          const x = LEFT + i * slot + (slot - barW) / 2
          const h = (v / scaleMax) * plotH
          const lo = TEFF_MIN + (i / TEFF_BINS) * (TEFF_MAX - TEFF_MIN)
          const hi = lo + (TEFF_MAX - TEFF_MIN) / TEFF_BINS
          return (
            <g
              key={i}
              onPointerEnter={(e) => {
                const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                show(((x + barW / 2) / W) * rect.width, ((TOP + plotH - h) / H) * rect.height, `T_eff ${fmtNum(lo)}–${fmtNum(hi)}`, [
                  { label: 'agent-ticks', value: String(v), color: SERIES[0] },
                ])
              }}
              onPointerLeave={hide}
            >
              <rect x={LEFT + i * slot} y={TOP} width={slot} height={plotH} fill="transparent" />
              <path d={columnPath(x, TOP + plotH - h, barW, h)} fill={SERIES[0]} />
            </g>
          )
        })}
        {collapse.map((c) => (
          <g key={c.tier}>
            <line x1={xOf(c.tC)} x2={xOf(c.tC)} y1={TOP} y2={TOP + plotH} stroke={c.color} strokeWidth="1" opacity="0.9" />
            <text x={xOf(c.tC) + 3} y={TOP + 10} fill={INK.secondary} fontSize="9">
              T_c {c.tier}
            </text>
          </g>
        ))}
        <line x1={LEFT} x2={W - 8} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
        {[0.5, 0.9, 1.3, 1.7, 2.1].map((t) => (
          <text key={t} x={xOf(t)} y={H - 8} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
            {t.toFixed(1)}
          </text>
        ))}
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
})
