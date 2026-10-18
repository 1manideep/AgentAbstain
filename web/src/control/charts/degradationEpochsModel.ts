/**
 * The data side of the degradation-vs-stress chart and the epochs strip: what they plot, computed from the series and
 * the snapshot's epochs. The chart modules (DegradationVsStress.tsx, EpochsStrip.tsx) hold only components, so fast
 * refresh can treat them as component modules.
 */
import type { Epoch, HelloConfig } from '../../protocol'
import { DEGRADATION_BINS, STRESS_BINS, type DegradationBin, type EpochSpan, type StressBinAcc } from '../../state/series'

// ------------------------------------------------------------------ degradation vs stress

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

export interface DegradationSeries {
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

// ------------------------------------------------------------------ epochs

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
