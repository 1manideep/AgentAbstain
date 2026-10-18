import { memo } from 'react'
import type { HelloConfig } from '../../protocol'
import { DEGRADATION_BINS, STRESS_BINS, type DegradationBin, type StressBinAcc } from '../../state/series'
import { fmtNum, fmtPct, INK, linePath } from './chartTheme'
import { ChartTip, useChartTip } from './ChartTip'

/** DESIGN §8: with induce/both the degeneration charts check the operator's calibration; they are not findings. */
export const CALIBRATION_LABEL = 'induced: calibration check, not a finding'

/**
 * The calibration label for a run's degeneration mode: null in `observe` mode, the label for `induce`, `both`
 * and a config without the field (the server defaults it to `both`). Null before hello, when nothing is known.
 */
export function calibrationLabel(config: Pick<HelloConfig, 'degeneration_mode'> | null | undefined): string | null {
  if (!config) return null
  return config.degeneration_mode === 'observe' ? null : CALIBRATION_LABEL
}

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

export interface TierCurve {
  tier: string
  color: string
  /** mean per stress bin, NaN when empty */
  coherence: number[]
  invalid: number[]
  /** Weight behind each bin: decide calls (per-call bins) or metrics rows (fallback). */
  n: number[]
  /** Calls with a coherence score per bin (per-call bins); the rows with a coherence mean in the fallback. */
  scored: number[]
}

/** 'calls': per-call stress bins, call-weighted. 'rows': each tier's tick in the bin of its mean stress (older rows). */
export type DegradationSource = 'calls' | 'rows'

export interface DegradationView {
  source: DegradationSource
  bins: number
  curves: TierCurve[]
  /** Calls behind the plot ('calls'), or tier-rows ('rows'). */
  total: number
}

interface DegradationSeries {
  degradationBins: ReadonlyMap<string, readonly DegradationBin[]>
  coherenceByTierByStressBin: ReadonlyMap<string, StressBinAcc>
}

/**
 * Per tier, mean text coherence and invalid-action rate per stress bin. From the per-call stress bins when any row
 * carried them (coherence = coherence_sum / coherence_n, invalid = invalid_sum / calls, both pooled over rows, so
 * weighted by calls); otherwise from the older mean-stress structure. Tiers with no data are left out.
 */
export function degradationView(series: DegradationSeries, colorOf: (tier: string) => string): DegradationView {
  const curves: TierCurve[] = []
  let total = 0
  if (series.degradationBins.size > 0) {
    for (const [tier, acc] of series.degradationBins) {
      const c: TierCurve = { tier, color: colorOf(tier), coherence: [], invalid: [], n: [], scored: [] }
      let weight = 0
      for (let b = 0; b < DEGRADATION_BINS; b++) {
        const d = acc[b]
        const calls = d?.calls ?? 0
        const cohN = d?.coherence_n ?? 0
        c.coherence.push(cohN > 0 ? d!.coherence_sum / cohN : NaN)
        c.invalid.push(calls > 0 ? d!.invalid_sum / calls : NaN)
        c.n.push(calls)
        c.scored.push(cohN)
        weight += calls
      }
      if (weight > 0) {
        curves.push(c)
        total += weight
      }
    }
    curves.sort((a, b) => a.tier.localeCompare(b.tier))
    return { source: 'calls', bins: DEGRADATION_BINS, curves, total }
  }
  for (const [tier, acc] of series.coherenceByTierByStressBin) {
    const c: TierCurve = { tier, color: colorOf(tier), coherence: [], invalid: [], n: [], scored: [] }
    let weight = 0
    for (let b = 0; b < STRESS_BINS; b++) {
      const cohN = acc.cohN[b] ?? 0
      const invN = acc.invN[b] ?? 0
      c.coherence.push(cohN > 0 ? acc.cohSum[b]! / cohN : NaN)
      c.invalid.push(invN > 0 ? acc.invSum[b]! / invN : NaN)
      c.n.push(Math.max(cohN, invN))
      c.scored.push(cohN)
      weight += Math.max(cohN, invN)
    }
    if (weight > 0) {
      curves.push(c)
      total += weight
    }
  }
  curves.sort((a, b) => a.tier.localeCompare(b.tier))
  return { source: 'rows', bins: STRESS_BINS, curves, total }
}

export type DegradationMetric = 'coherence' | 'invalid'

/** Tooltip label of one point: the tier and the counts behind it (calls, or rows in the fallback). */
export function pointLabel(curve: TierCurve, bin: number, metric: DegradationMetric, source: DegradationSource): string {
  const n = curve.n[bin] ?? 0
  if (source === 'rows') return `${curve.tier} · ${n} ${n === 1 ? 'row' : 'rows'}`
  const calls = `${n} ${n === 1 ? 'call' : 'calls'}`
  return metric === 'coherence' ? `${curve.tier} · ${curve.scored[bin] ?? 0} scored of ${calls}` : `${curve.tier} · ${calls}`
}

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
