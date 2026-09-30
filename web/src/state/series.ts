/**
 * Incremental chart aggregates (DESIGN §16 `series`). Every reducer is O(1) in
 * the number of ticks; the chart components read these objects at ≤ 4 Hz.
 */
import type { DayRow, EventMsg, MetricsRow, RosterAgent, SnapshotMsg } from '../protocol'
import { isEventKind } from '../protocol'

export const TEFF_MIN = 0.5
export const TEFF_MAX = 2.1
export const TEFF_BINS = 16
export const TEFF_WINDOW = 240
export const TICK_WINDOW = 720
export const STRESS_BINS = 5
export const MAX_GENERATIONS = 12

/** Fixed-capacity per-tick ring of numbers. */
export class TickRing {
  readonly capacity: number
  ticks: Int32Array
  values: Float32Array
  head = 0
  count = 0
  constructor(capacity = TICK_WINDOW) {
    this.capacity = capacity
    this.ticks = new Int32Array(capacity)
    this.values = new Float32Array(capacity)
  }
  push(tick: number, v: number): void {
    const i = (this.head + this.count) % this.capacity
    if (this.count === this.capacity) {
      this.ticks[this.head] = tick
      this.values[this.head] = v
      this.head = (this.head + 1) % this.capacity
    } else {
      this.ticks[i] = tick
      this.values[i] = v
      this.count++
    }
  }
  tickAt(i: number): number {
    return this.ticks[(this.head + i) % this.capacity]!
  }
  valueAt(i: number): number {
    return this.values[(this.head + i) % this.capacity]!
  }
  clear(): void {
    this.head = 0
    this.count = 0
  }
}

export interface StressBinAcc {
  cohSum: Float64Array
  cohN: Int32Array
  invSum: Float64Array
  invN: Int32Array
}

export interface EpochRecord {
  seq: number
  tick: number
  day: number
  kind: string
  phase: string
  duration_days: number | null
  scarcity: number | null
  weather_baseline: number | null
}

export interface GossipEdge {
  from: string
  to: string
  count: number
  lastTick: number
}

export class Series {
  // T_eff histogram over the last 240 ticks (counts per bin; ring of per-tick samples)
  teffBins = new Int32Array(TEFF_BINS)
  private teffRing: Float32Array[] = []
  private teffRingHead = 0
  teffSamples = 0

  degenByGen = new Int32Array(MAX_GENERATIONS)
  degenTotal = 0

  popByTick = new TickRing()
  meanBalanceByTick = new TickRing()
  giniByTick = new TickRing()
  gini = 0
  /** population by generation per tick (ring of Uint8Array(MAX_GENERATIONS)). */
  popByGenRing: Uint8Array[] = []
  popByGenTicks = new Int32Array(TICK_WINDOW)
  popByGenHead = 0
  popByGenCount = 0
  maxGeneration = 0

  coherenceByTierByStressBin = new Map<string, StressBinAcc>()
  falseClaimByTier = new Map<string, { made: number; falsy: number }>()
  gossipEdges = new Map<string, GossipEdge>()
  gossipTotal = 0
  epochs: EpochRecord[] = []
  daily: DayRow[] = []
  lastMetricsTick = -1
  metricsRows = 0

  clear(): void {
    this.teffBins.fill(0)
    this.teffRing = []
    this.teffRingHead = 0
    this.teffSamples = 0
    this.degenByGen.fill(0)
    this.degenTotal = 0
    this.popByTick.clear()
    this.meanBalanceByTick.clear()
    this.giniByTick.clear()
    this.gini = 0
    this.popByGenRing = []
    this.popByGenHead = 0
    this.popByGenCount = 0
    this.maxGeneration = 0
    this.coherenceByTierByStressBin.clear()
    this.falseClaimByTier.clear()
    this.gossipEdges.clear()
    this.gossipTotal = 0
    this.epochs = []
    this.daily = []
    this.lastMetricsTick = -1
    this.metricsRows = 0
  }

  private teffBin(v: number): number {
    const k = Math.floor(((v - TEFF_MIN) / (TEFF_MAX - TEFF_MIN)) * TEFF_BINS)
    return k < 0 ? 0 : k >= TEFF_BINS ? TEFF_BINS - 1 : k
  }

  /** Per snapshot: T_eff histogram window, population, mean balance, gini, generations. */
  onSnapshot(snap: SnapshotMsg, rosterById: ReadonlyMap<string, RosterAgent>): void {
    const alive = snap.agents.filter((a) => a.status === 'alive')
    const vals = new Float32Array(alive.length)
    let bal = 0
    for (let i = 0; i < alive.length; i++) {
      vals[i] = alive[i]!.t_eff
      bal += alive[i]!.balance_usd
    }
    // T_eff window
    if (this.teffRing.length < TEFF_WINDOW) {
      this.teffRing.push(vals)
    } else {
      const old = this.teffRing[this.teffRingHead]!
      for (let i = 0; i < old.length; i++) this.teffBins[this.teffBin(old[i]!)]!--
      this.teffSamples -= old.length
      this.teffRing[this.teffRingHead] = vals
      this.teffRingHead = (this.teffRingHead + 1) % TEFF_WINDOW
    }
    for (let i = 0; i < vals.length; i++) this.teffBins[this.teffBin(vals[i]!)]!++
    this.teffSamples += vals.length

    this.popByTick.push(snap.tick, alive.length)
    this.meanBalanceByTick.push(snap.tick, alive.length ? bal / alive.length : 0)

    // Gini over alive balances (n ≤ population cap, so the sort is negligible).
    const sorted = alive.map((a) => a.balance_usd).sort((a, b) => a - b)
    let num = 0
    let sum = 0
    for (let i = 0; i < sorted.length; i++) {
      num += (2 * (i + 1) - sorted.length - 1) * sorted[i]!
      sum += sorted[i]!
    }
    this.gini = sorted.length > 1 && sum > 0 ? num / (sorted.length * sum) : 0
    this.giniByTick.push(snap.tick, this.gini)

    // Population by generation
    const byGen = new Uint8Array(MAX_GENERATIONS)
    for (const a of alive) {
      const g = rosterById.get(a.id)?.generation ?? 0
      const gi = g >= MAX_GENERATIONS ? MAX_GENERATIONS - 1 : g
      byGen[gi] = (byGen[gi]! + 1) & 0xff
      if (g > this.maxGeneration) this.maxGeneration = g
    }
    if (this.popByGenCount < TICK_WINDOW) {
      const i = (this.popByGenHead + this.popByGenCount) % TICK_WINDOW
      this.popByGenRing[i] = byGen
      this.popByGenTicks[i] = snap.tick
      this.popByGenCount++
    } else {
      this.popByGenRing[this.popByGenHead] = byGen
      this.popByGenTicks[this.popByGenHead] = snap.tick
      this.popByGenHead = (this.popByGenHead + 1) % TICK_WINDOW
    }
  }

  popByGenAt(i: number): { tick: number; byGen: Uint8Array } {
    const k = (this.popByGenHead + i) % TICK_WINDOW
    return { tick: this.popByGenTicks[k]!, byGen: this.popByGenRing[k]! }
  }

  /** Per metrics row: observed degradation vs stress bin per tier. */
  onMetrics(row: MetricsRow): void {
    if (row.tick <= this.lastMetricsTick) return
    this.lastMetricsTick = row.tick
    this.metricsRows++
    for (const tier of Object.keys(row.by_tier)) {
      const agg = row.by_tier[tier]!
      if (!agg || agg.population <= 0) continue
      let acc = this.coherenceByTierByStressBin.get(tier)
      if (!acc) {
        acc = {
          cohSum: new Float64Array(STRESS_BINS),
          cohN: new Int32Array(STRESS_BINS),
          invSum: new Float64Array(STRESS_BINS),
          invN: new Int32Array(STRESS_BINS),
        }
        this.coherenceByTierByStressBin.set(tier, acc)
      }
      let b = Math.floor(agg.mean_stress * STRESS_BINS)
      if (b < 0) b = 0
      if (b >= STRESS_BINS) b = STRESS_BINS - 1
      if (typeof agg.text_coherence_mean === 'number') {
        acc.cohSum[b]! += agg.text_coherence_mean
        acc.cohN[b]!++
      }
      if (typeof agg.invalid_action_rate === 'number') {
        acc.invSum[b]! += agg.invalid_action_rate
        acc.invN[b]!++
      }
    }
  }

  onDay(row: DayRow): void {
    const i = this.daily.findIndex((d) => d.day === row.day)
    if (i >= 0) this.daily[i] = row
    else this.daily.push(row)
    if (this.daily.length > 400) this.daily.shift()
  }

  /** Per event: degeneration per generation, false claims per tier, gossip graph, epochs. */
  onEvent(e: EventMsg, rosterById: ReadonlyMap<string, RosterAgent>): void {
    if (isEventKind(e, 'degeneration')) {
      const g = e.payload.generation
      const gi = g < 0 ? 0 : g >= MAX_GENERATIONS ? MAX_GENERATIONS - 1 : g
      this.degenByGen[gi]!++
      this.degenTotal++
      return
    }
    if (isEventKind(e, 'claim')) {
      const tier = rosterById.get(e.payload.agent_id)?.tier ?? 'unknown'
      let c = this.falseClaimByTier.get(tier)
      if (!c) {
        c = { made: 0, falsy: 0 }
        this.falseClaimByTier.set(tier, c)
      }
      c.made++
      if (!e.payload.truthful) c.falsy++
      return
    }
    if (isEventKind(e, 'gossip_transfer')) {
      const key = e.payload.speaker_id + '>' + e.payload.listener_id
      const edge = this.gossipEdges.get(key)
      if (edge) {
        edge.count++
        edge.lastTick = e.tick
      } else {
        this.gossipEdges.set(key, { from: e.payload.speaker_id, to: e.payload.listener_id, count: 1, lastTick: e.tick })
      }
      this.gossipTotal++
      return
    }
    if (isEventKind(e, 'epoch')) {
      const p = e.payload
      this.epochs.push({
        seq: e.seq,
        tick: e.tick,
        day: e.day,
        kind: String(p.kind),
        phase: typeof p.phase === 'string' ? p.phase : 'apply',
        duration_days: typeof p.duration_days === 'number' ? p.duration_days : null,
        scarcity: typeof p.scarcity === 'number' ? p.scarcity : null,
        weather_baseline: typeof p.weather_baseline === 'number' ? p.weather_baseline : null,
      })
      if (this.epochs.length > 48) this.epochs.shift()
    }
  }
}
