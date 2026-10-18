import { memo } from 'react'
import type { HelloConfig } from '../../protocol'
import { fmtNum, fmtPct, INK, linePath } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'
import { calibrationLabel, pointLabel, type DegradationMetric, type DegradationView } from './degradationEpochsModel'

/**
 * Compatibility for degradationEpochs.test.tsx, which still imports these from here: the model lives in
 * degradationEpochsModel.ts. Remove once that test imports from the model.
 */
// oxlint-disable-next-line react/only-export-components
export { CALIBRATION_LABEL, calibrationLabel, degradationView, pointLabel } from './degradationEpochsModel'

/** The label as a note at the top of a card body; renders nothing in `observe` mode. */
export const CalibrationNote = memo(function CalibrationNote({ config }: { config: Pick<HelloConfig, 'degeneration_mode'> | null }) {
  const label = calibrationLabel(config)
  if (!label) return null
  return (
    <div role="note" style={{ margin: '6px 0 2px' }}>
      <span className="pill warn">{label}</span>
    </div>
  )
})

/** Runs of consecutive bins with a value; a line never bridges an empty bin. */
function runs(vals: readonly number[]): number[][] {
  const out: number[][] = []
  let cur: number[] = []
  vals.forEach((v, b) => {
    if (Number.isFinite(v)) cur.push(b)
    else if (cur.length) {
      out.push(cur)
      cur = []
    }
  })
  if (cur.length) out.push(cur)
  return out
}

const W = 360
const H = 120
const LEFT = 34
const RIGHT = 12
const TOP = 10
const BOTTOM = 22
const X_TICKS = [0, 0.2, 0.4, 0.6, 0.8, 1]

interface PanelProps {
  title: string
  view: DegradationView
  metric: DegradationMetric
  fmt: (v: number) => string
}

function Panel({ title, view, metric, fmt }: PanelProps) {
  const { tip, show, hide } = useChartTip()
  const { bins, curves, source } = view
  const plotW = W - LEFT - RIGHT
  const plotH = H - TOP - BOTTOM
  const xOfStress = (s: number) => LEFT + s * plotW
  const xOf = (b: number) => xOfStress((b + 0.5) / bins)
  const yOf = (v: number) => TOP + plotH - Math.min(1, Math.max(0, v)) * plotH
  const binName = (b: number) => `stress ${(b / bins).toFixed(1)}–${((b + 1) / bins).toFixed(1)}`
  return (
    <div className="chart">
      <div className="chart-sub">{title}</div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={title}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={LEFT} x2={W - RIGHT} y1={yOf(f)} y2={yOf(f)} stroke={INK.grid} strokeWidth="1" />
            <text x={LEFT - 6} y={yOf(f) + 3} fill={INK.muted} fontSize="10" textAnchor="end" className="tnum">
              {fmt(f)}
            </text>
          </g>
        ))}
        {X_TICKS.map((s) => (
          <g key={s}>
            <line x1={xOfStress(s)} x2={xOfStress(s)} y1={TOP + plotH} y2={TOP + plotH + 3} stroke={INK.axis} strokeWidth="1" />
            <text x={xOfStress(s)} y={H - 6} fill={INK.muted} fontSize="10" textAnchor="middle" className="tnum">
              {s === 0 || s === 1 ? String(s) : s.toFixed(1)}
            </text>
          </g>
        ))}
        {curves.map((c) => {
          const vals = metric === 'coherence' ? c.coherence : c.invalid
          return (
            <g key={c.tier}>
              {runs(vals).map((r) => (
                <path key={r[0]} d={linePath(r.map((b) => [xOf(b), yOf(vals[b]!)]))} fill="none" stroke={c.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              ))}
              {vals.map((v, b) =>
                Number.isFinite(v) ? (
                  <g
                    key={b}
                    onPointerEnter={(e) => {
                      const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect()
                      show((xOf(b) / W) * rect.width, (yOf(v) / H) * rect.height, binName(b), [{ label: pointLabel(c, b, metric, source), value: fmt(v), color: c.color }])
                    }}
                    onPointerLeave={hide}
                  >
                    <circle cx={xOf(b)} cy={yOf(v)} r="9" fill="transparent" />
                    <circle cx={xOf(b)} cy={yOf(v)} r="4" fill={c.color} stroke={INK.surface} strokeWidth="2" />
                  </g>
                ) : null,
              )}
            </g>
          )
        })}
        <line x1={LEFT} x2={W - RIGHT} y1={TOP + plotH} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" />
      </svg>
      <ChartTip tip={tip} width={W} />
    </div>
  )
}

/** Observed degradation (text coherence, invalid-action rate) vs stress bin per tier — the EIML3 comparison chart. */
export const DegradationVsStress = memo(function DegradationVsStress({ view }: { view: DegradationView }) {
  if (view.curves.length === 0) return <div className="muted small">Waiting for metrics rows.</div>
  const calls = view.source === 'calls'
  return (
    <div>
      <div className="chart-legend">
        {view.curves.map((c) => (
          <span className="legend-item" key={c.tier}>
            <span className="legend-line" style={{ background: c.color }} />
            {c.tier}
          </span>
        ))}
      </div>
      <div className="chart-sub">
        {calls
          ? `x: stress of each decide call, ${view.bins} bins; points weighted by calls`
          : `x: tier mean stress per tick, ${view.bins} bins (rows carry no per-call bins)`}
      </div>
      <Panel title={calls ? 'Text coherence (mean over scored calls)' : 'Text coherence (mean)'} view={view} metric="coherence" fmt={(v) => fmtNum(v, 2)} />
      <Panel title={calls ? 'Invalid action rate (over calls)' : 'Invalid action rate'} view={view} metric="invalid" fmt={(v) => fmtPct(v)} />
    </div>
  )
})
