/**
 * Incremental chart aggregates (DESIGN §16 `series`). Every reducer is O(1) in
 * the number of ticks; the chart components read these objects at ≤ 4 Hz.
 *
 * Replays are safe: snapshots at or below the newest counted tick are ignored
 * (DESIGN §15 replays the last 60 on every reconnect), metrics rows likewise,
 * and events are deduplicated by seq before they reach `onEvent` (EventRing).
 *
 * Event-derived aggregates (degeneration per generation, T_c per tier, false claims per tier, the gossip graph,
 * epochs, the memory accumulators and the gadget gate events) cover the whole run, not only the 2000-event ring:
 * a fresh tab seeds them from GET /api/events/aggregates (`seedEvents`), which counts every event up to its
 * `as_of_seq`, and `onEvent` then counts only events with a larger seq, so the seam counts nothing twice.
 */
import type { DayRow, Epoch, EventAggregatesResponse, EventMsg, MemoryAggregate, MetricsRow, RosterAgent, SnapshotMsg } from '../protocol'
import { isEventKind } from '../protocol'

export const TEFF_MIN = 0.5
export const TEFF_MAX = 2.1
export const TEFF_BINS = 16
export const TEFF_WINDOW = 240
export const TICK_WINDOW = 720
export const STRESS_BINS = 5
export const MAX_GENERATIONS = 12
/** Per-call stress bins in metrics rows (by_tier.<t>.stress_bins): lo = 0.0 .. 0.9. */
export const DEGRADATION_BINS = 10
/** Epoch log length (started and ended records). */
export const MAX_EPOCH_RECORDS = 48
/** Epochs kept by id. */
export const MAX_EPOCH_SPANS = 256
/** gadget_verified / gadget_rejected events kept for the proposals list (GATE_EVENTS_LIMIT in void/server/api.py). */
export const MAX_GATE_EVENTS = 200

const MICRO = 1_000_000

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

const genIndex = (g: number): number => (g < 0 ? 0 : g >= MAX_GENERATIONS ? MAX_GENERATIONS - 1 : g)

/** A claim's tier: the roster's tier for the speaker, else the payload's, else 'unknown' (live and seeded alike). */
export function claimTier(agentId: unknown, payloadTier: unknown, rosterById: ReadonlyMap<string, RosterAgent>): string {
  return (typeof agentId === 'string' ? rosterById.get(agentId)?.tier : undefined) ?? str(payloadTier) ?? 'unknown'
}

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

/** Call-weighted totals of one stress bin for one tier, summed over metrics rows. */
export interface DegradationBin {
  calls: number
  coherence_sum: number
  coherence_n: number
  invalid_sum: number
}

/** One epoch event (or a snapshot's active epoch standing in for a started event the client never saw). */
export interface EpochRecord {
  /** Event seq; negative for a record merged from a snapshot. */
  seq: number
  tick: number
  day: number
  kind: string
  phase: string
  /** From the payload when present, else ends_day - day; null when neither is known (an ended event). */
  duration_days: number | null
  scarcity: number | null
  weather_baseline: number | null
  /** Server epoch id ('cfg0', 'op1', ...); null on payloads without one. */
  id: string | null
  /** The day the epoch expires: it covers day .. ends_day - 1. Null when the payload had none. */
  ends_day: number | null
}

export type EpochState = 'active' | 'past'

/**
 * One epoch keyed by id: its start (the started event, or a snapshot's active epoch when the client connected
 * mid-epoch) merged with its ended event. `day` is the first day and `ends_day` the expiry day.
 */
export interface EpochSpan extends EpochRecord {
  id: string
  /** Day of the ended event; null while the epoch runs. */
  ended_day: number | null
  state: EpochState
  arrival_agent_id: string | null
  /** 'event': a started event was seen; 'snapshot': only a snapshot's active epoch; 'ended': only the end. */
  source: 'event' | 'snapshot' | 'ended'
}

export interface GossipEdge {
  from: string
  to: string
  count: number
  lastTick: number
}

/** Memory-layer totals for one agent, from maintenance and exam_answered events. */
export interface AgentMemoryAcc {
  /** Maintenance cost in USD (the payload carries micro-dollars), whoever paid: the micro sums below over 1e6. */
  maintenanceReal: number
  maintenanceWorld: number
  /** World cost the agent paid from its own wallet (payer is the agent, not the research pool). */
  maintenanceWorldSelf: number
  /** The same costs summed in micro-dollars (exact, so a seeded sum equals a live one). */
  maintenanceRealMicro: number
  maintenanceWorldMicro: number
  maintenanceWorldSelfMicro: number
  maintenanceCalls: number
  maintenanceSkipped: number
  /** Graded audit items; failed calls are kept out, as the server's ExamReport does. */
  asked: number
  correct: number
  abstained: number
  failed: number
}

export interface ExamTally {
  asked: number
  correct: number
  abstained: number
  failed: number
}

const emptyAgentMemory = (): AgentMemoryAcc => ({
  maintenanceReal: 0,
  maintenanceWorld: 0,
  maintenanceWorldSelf: 0,
  maintenanceRealMicro: 0,
  maintenanceWorldMicro: 0,
  maintenanceWorldSelfMicro: 0,
  maintenanceCalls: 0,
  maintenanceSkipped: 0,
  asked: 0,
  correct: 0,
  abstained: 0,
  failed: 0,
})

/** Memory spend and exam scores per agent and per day (MEMORY_EVOLUTION §5.6, §5.8). */
export class MemorySeries {
  byAgent = new Map<string, AgentMemoryAcc>()
  /** maintenance_skipped events per day. */
  skippedByDay = new Map<number, number>()
  /** exam_answered per day, then per question kind. */
  examByDay = new Map<number, Map<string, ExamTally>>()
  maintenanceReal = 0
  maintenanceWorld = 0
  maintenanceCalls = 0
  maintenanceSkipped = 0
  private realMicro = 0
  private worldMicro = 0

  clear(): void {
    this.byAgent.clear()
    this.skippedByDay.clear()
    this.examByDay.clear()
    this.maintenanceReal = 0
    this.maintenanceWorld = 0
    this.maintenanceCalls = 0
    this.maintenanceSkipped = 0
    this.realMicro = 0
    this.worldMicro = 0
  }

  /** Replace every total with the server's whole-run aggregate (GET /api/events/aggregates `memory`). */
  seed(m: MemoryAggregate): void {
    this.clear()
    for (const r of m.agents) {
      const a = this.agent(r.agent_id)
      a.maintenanceRealMicro = r.maintenance_real_micro
      a.maintenanceWorldMicro = r.maintenance_world_micro
      a.maintenanceWorldSelfMicro = r.maintenance_world_self_micro
      a.maintenanceReal = a.maintenanceRealMicro / MICRO
      a.maintenanceWorld = a.maintenanceWorldMicro / MICRO
      a.maintenanceWorldSelf = a.maintenanceWorldSelfMicro / MICRO
      a.maintenanceCalls = r.maintenance_calls
      a.maintenanceSkipped = r.maintenance_skipped
      a.asked = r.asked
      a.correct = r.correct
      a.abstained = r.abstained
      a.failed = r.failed
      this.realMicro += r.maintenance_real_micro
      this.worldMicro += r.maintenance_world_micro
      this.maintenanceCalls += r.maintenance_calls
    }
    this.maintenanceReal = this.realMicro / MICRO
    this.maintenanceWorld = this.worldMicro / MICRO
    for (const d of m.skipped_by_day) {
      this.skippedByDay.set(d.day, (this.skippedByDay.get(d.day) ?? 0) + d.count)
      this.maintenanceSkipped += d.count
    }
    for (const x of m.exams) {
      let byKind = this.examByDay.get(x.day)
      if (!byKind) {
        byKind = new Map()
        this.examByDay.set(x.day, byKind)
      }
      const t = byKind.get(x.kind) ?? { asked: 0, correct: 0, abstained: 0, failed: 0 }
      byKind.set(x.kind, { asked: t.asked + x.asked, correct: t.correct + x.correct, abstained: t.abstained + x.abstained, failed: t.failed + x.failed })
    }
  }

  agent(id: string): AgentMemoryAcc {
    let a = this.byAgent.get(id)
    if (!a) {
      a = emptyAgentMemory()
      this.byAgent.set(id, a)
    }
    return a
  }

  onMaintenance(agentId: string, p: Record<string, unknown>): void {
    const real = num(p.real_cost) ?? 0
    const world = num(p.world_cost) ?? 0
    const a = this.agent(agentId)
    a.maintenanceRealMicro += real
    a.maintenanceWorldMicro += world
    a.maintenanceReal = a.maintenanceRealMicro / MICRO
    a.maintenanceWorld = a.maintenanceWorldMicro / MICRO
    if (p.payer === agentId) {
      a.maintenanceWorldSelfMicro += world
      a.maintenanceWorldSelf = a.maintenanceWorldSelfMicro / MICRO
    }
    a.maintenanceCalls++
    this.realMicro += real
    this.worldMicro += world
    this.maintenanceReal = this.realMicro / MICRO
    this.maintenanceWorld = this.worldMicro / MICRO
    this.maintenanceCalls++
  }

  onMaintenanceSkipped(agentId: string | null, day: number): void {
    this.skippedByDay.set(day, (this.skippedByDay.get(day) ?? 0) + 1)
    this.maintenanceSkipped++
    if (agentId) this.agent(agentId).maintenanceSkipped++
  }

  onExamAnswered(agentId: string | null, day: number, p: Record<string, unknown>): void {
    const kind = str(p.kind) ?? 'unknown'
    let byKind = this.examByDay.get(day)
    if (!byKind) {
      byKind = new Map()
      this.examByDay.set(day, byKind)
    }
    let t = byKind.get(kind)
    if (!t) {
      t = { asked: 0, correct: 0, abstained: 0, failed: 0 }
      byKind.set(kind, t)
    }
    const a = agentId ? this.agent(agentId) : null
    if (p.failed === true) {
      t.failed++
      if (a) a.failed++
      return
    }
    const correct = p.correct === true ? 1 : 0
    const abstained = p.abstained === true ? 1 : 0
    t.asked++
    t.correct += correct
    t.abstained += abstained
    if (a) {
      a.asked++
      a.correct += correct
      a.abstained += abstained
    }
  }
}

export class Series {
  // T_eff histogram over the last 240 ticks (counts per bin; ring of per-tick samples)
  teffBins = new Int32Array(TEFF_BINS)
  private teffRing: Float32Array[] = []
  private teffRingHead = 0
  teffSamples = 0

  degenByGen = new Int32Array(MAX_GENERATIONS)
  degenTotal = 0
  /** Collapse temperature per tier, learned from degeneration payloads. */
  tCByTier = new Map<string, number>()

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

  /** Mean-stress binning of each tier's per-tick means; kept until the chart reads `degradationBins`. */
  coherenceByTierByStressBin = new Map<string, StressBinAcc>()
  /** Per-call stress bins per tier (DEGRADATION_BINS each), summed over metrics rows, so call-weighted. */
  degradationBins = new Map<string, DegradationBin[]>()
  falseClaimByTier = new Map<string, { made: number; falsy: number }>()
  gossipEdges = new Map<string, GossipEdge>()
  gossipTotal = 0
  /** Epoch event log (started / ended), oldest first. */
  epochs: EpochRecord[] = []
  /** Epochs by id, merged from started events, snapshots' active epoch and ended events. */
  epochsById = new Map<string, EpochSpan>()
  daily: DayRow[] = []
  lastMetricsTick = -1
  metricsRows = 0
  /** Run the aggregates belong to; a different run id clears them (beginRun). */
  runId: string | null = null
  /** Newest snapshot tick counted; replayed snapshots at or below it are ignored. */
  lastSnapshotTick = -1
  snapshotsCounted = 0
  memory = new MemorySeries()
  /** gadget_verified / gadget_rejected events, oldest first, the newest MAX_GATE_EVENTS (the proposals list). */
  gateEvents: EventMsg[] = []
  /** Bumped whenever `gateEvents` changes. */
  gateRev = 0
  /**
   * Events with seq at or below this were counted by a server aggregate (`seedEvents`); `onEvent` skips them.
   * 0 until a seed: then every event that reaches `onEvent` counts (fixture replay, a server without the route).
   */
  aggregatesSeq = 0

  /** Called on hello: a new run id clears every aggregate. Returns true when it cleared. */
  beginRun(runId: string): boolean {
    if (this.runId === runId) return false
    const had = this.runId !== null
    if (had) this.clear()
    this.runId = runId
    return had
  }

  clear(): void {
    this.runId = null
    this.lastSnapshotTick = -1
    this.snapshotsCounted = 0
    this.degradationBins.clear()
    this.epochsById.clear()
    this.memory.clear()
    this.gateEvents = []
    this.gateRev++
    this.aggregatesSeq = 0
    this.teffBins.fill(0)
    this.teffRing = []
    this.teffRingHead = 0
    this.teffSamples = 0
    this.degenByGen.fill(0)
    this.degenTotal = 0
    this.tCByTier.clear()
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

  /**
   * Per snapshot: T_eff histogram window, population, mean balance, gini, generations, and the active epoch.
   * Returns false (and changes nothing) for a snapshot at or below the newest counted tick.
   */
  onSnapshot(snap: SnapshotMsg, rosterById: ReadonlyMap<string, RosterAgent>): boolean {
    if (!(snap.tick > this.lastSnapshotTick)) return false
    this.lastSnapshotTick = snap.tick
    this.snapshotsCounted++
    this.mergeSnapshotEpochs(snap)
    const alive = snap.agents.filter((a) => a.status === 'alive')
    const withCall = alive.filter((a) => typeof a.t_eff === 'number' && Number.isFinite(a.t_eff))
    const vals = new Float32Array(withCall.length)
    let bal = 0
    for (let i = 0; i < withCall.length; i++) vals[i] = withCall[i]!.t_eff as number
    for (const a of alive) bal += a.balance_usd
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
    return true
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
    for (const tier of Object.keys(row.by_tier ?? {})) {
      const agg = row.by_tier[tier]!
      if (agg && Array.isArray(agg.stress_bins)) this.addStressBins(tier, agg.stress_bins)
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
      if (typeof agg.mean_stress !== 'number') continue
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

  /** Sum one row's per-call stress bins for a tier. Bins are placed by `lo` (array position as a fallback). */
  private addStressBins(tier: string, bins: readonly unknown[]): void {
    let acc = this.degradationBins.get(tier)
    for (let i = 0; i < bins.length; i++) {
      const b = bins[i]
      if (!b || typeof b !== 'object') continue
      const o = b as Record<string, unknown>
      const calls = num(o.calls) ?? 0
      const cohN = num(o.coherence_n) ?? 0
      if (calls <= 0 && cohN <= 0) continue
      const lo = num(o.lo)
      let k = lo !== null ? Math.round(lo * DEGRADATION_BINS) : i
      if (k < 0) k = 0
      if (k >= DEGRADATION_BINS) k = DEGRADATION_BINS - 1
      if (!acc) {
        acc = Array.from({ length: DEGRADATION_BINS }, () => ({ calls: 0, coherence_sum: 0, coherence_n: 0, invalid_sum: 0 }))
        this.degradationBins.set(tier, acc)
      }
      const d = acc[k]!
      d.calls += calls
      d.coherence_sum += num(o.coherence_sum) ?? 0
      d.coherence_n += cohN
      d.invalid_sum += num(o.invalid_sum) ?? 0
    }
  }

  /** Call-weighted means of one tier's stress bin: coherence over scored calls, invalid actions over calls. */
  degradationAt(tier: string, bin: number): { calls: number; coherence: number | null; invalidRate: number | null } {
    const d = this.degradationBins.get(tier)?.[bin]
    if (!d) return { calls: 0, coherence: null, invalidRate: null }
    return {
      calls: d.calls,
      coherence: d.coherence_n > 0 ? d.coherence_sum / d.coherence_n : null,
      invalidRate: d.calls > 0 ? d.invalid_sum / d.calls : null,
    }
  }

  onDay(row: DayRow): void {
    const i = this.daily.findIndex((d) => d.day === row.day)
    if (i >= 0) this.daily[i] = row
    else this.daily.push(row)
    if (this.daily.length > 400) this.daily.shift()
  }

  /**
   * Replace every event-derived aggregate with the server's whole-run totals over the events with
   * seq <= `agg.as_of_seq`; from here on `onEvent` counts only later events. Claims take the speaker's tier
   * from the roster as they do live, so call this once the roster is in.
   */
  seedEvents(agg: EventAggregatesResponse, rosterById: ReadonlyMap<string, RosterAgent>): void {
    this.degenByGen.fill(0)
    this.degenTotal = 0
    this.tCByTier.clear()
    for (const r of agg.degeneration.by_generation) {
      this.degenTotal += r.count
      if (Number.isInteger(r.generation)) this.degenByGen[genIndex(r.generation as number)]! += r.count
    }
    for (const t of agg.degeneration.t_c_by_tier) this.tCByTier.set(t.tier, t.t_c)

    this.falseClaimByTier.clear()
    for (const c of agg.claims) this.addClaims(claimTier(c.agent_id, c.tier, rosterById), c.made, c.false)

    this.gossipEdges.clear()
    this.gossipTotal = 0
    for (const g of agg.gossip) this.addGossip(g.speaker_id ?? '', g.listener_id ?? '', g.count, g.last_tick)

    this.epochs = []
    this.epochsById.clear()
    for (const e of agg.epochs) this.onEpochEvent(e.seq, e.tick, e.day, e.payload)

    this.memory.seed(agg.memory)

    this.gateEvents = agg.gate.slice(-MAX_GATE_EVENTS)
    this.gateRev++
    this.aggregatesSeq = agg.as_of_seq
  }

  private addClaims(tier: string, made: number, falsy: number): void {
    let c = this.falseClaimByTier.get(tier)
    if (!c) {
      c = { made: 0, falsy: 0 }
      this.falseClaimByTier.set(tier, c)
    }
    c.made += made
    c.falsy += falsy
  }

  private addGossip(from: string, to: string, count: number, tick: number): void {
    const key = from + '>' + to
    const edge = this.gossipEdges.get(key)
    if (edge) {
      edge.count += count
      edge.lastTick = tick
    } else {
      this.gossipEdges.set(key, { from, to, count, lastTick: tick })
    }
    this.gossipTotal += count
  }

  /**
   * Per event: degeneration per generation, false claims per tier, gossip graph, epochs, memory, gate outcomes.
   * Events at or below `aggregatesSeq` are already in the seeded totals and are skipped.
   */
  onEvent(e: EventMsg, rosterById: ReadonlyMap<string, RosterAgent>): void {
    if (e.seq <= this.aggregatesSeq) return
    if (isEventKind(e, 'degeneration')) {
      const g: unknown = e.payload.generation
      // an integer generation is a bar; anything else counts in the total only (as the server's aggregate does)
      if (Number.isInteger(g)) this.degenByGen[genIndex(g as number)]!++
      this.degenTotal++
      if (typeof e.payload.t_c === 'number' && Number.isFinite(e.payload.t_c) && str(e.payload.tier)) this.tCByTier.set(e.payload.tier, e.payload.t_c)
      return
    }
    if (isEventKind(e, 'claim')) {
      this.addClaims(claimTier(e.payload.agent_id, e.payload.tier, rosterById), 1, e.payload.truthful ? 0 : 1)
      return
    }
    if (isEventKind(e, 'gossip_transfer')) {
      const p: Record<string, unknown> = e.payload as unknown as Record<string, unknown>
      this.addGossip(typeof p.speaker_id === 'string' ? p.speaker_id : '', typeof p.listener_id === 'string' ? p.listener_id : '', 1, e.tick)
      return
    }
    if (isEventKind(e, 'epoch')) {
      this.onEpochEvent(e.seq, e.tick, e.day, e.payload)
      return
    }
    if (isEventKind(e, 'gadget_verified') || isEventKind(e, 'gadget_rejected')) {
      this.gateEvents.push(e)
      if (this.gateEvents.length > MAX_GATE_EVENTS) this.gateEvents.splice(0, this.gateEvents.length - MAX_GATE_EVENTS)
      this.gateRev++
      return
    }
    if (isEventKind(e, 'maintenance')) {
      const id = str(e.payload.agent_id) ?? e.agent_id
      if (id) this.memory.onMaintenance(id, e.payload)
      return
    }
    if (isEventKind(e, 'maintenance_skipped')) {
      this.memory.onMaintenanceSkipped(str(e.payload.agent_id) ?? e.agent_id, e.day)
      return
    }
    if (isEventKind(e, 'exam_answered')) {
      this.memory.onExamAnswered(str(e.payload.agent_id) ?? e.agent_id, e.day, e.payload)
    }
  }

  // ------------------------------------------------------------------ epochs

  private onEpochEvent(seq: number, tick: number, day: number, p: Record<string, unknown>): void {
    const phase = str(p.phase) ?? 'started'
    const id = str(p.id)
    const endsDay = num(p.ends_day)
    const rec: EpochRecord = {
      seq,
      tick,
      day,
      kind: String(p.kind),
      phase,
      duration_days: num(p.duration_days) ?? (endsDay !== null && phase !== 'ended' ? endsDay - day : null),
      scarcity: num(p.scarcity),
      weather_baseline: num(p.weather_baseline),
      id,
      ends_day: endsDay,
    }
    // A started event replaces the record a snapshot merged for the same epoch, so the log holds it once.
    const merged = id !== null && phase !== 'ended' ? this.epochs.findIndex((r) => r.id === id && r.seq < 0) : -1
    if (merged >= 0) this.epochs[merged] = rec
    else this.pushEpochRecord(rec)
    if (id === null) return

    const span = this.epochsById.get(id)
    if (phase === 'ended') {
      if (span) {
        span.ended_day = day
        span.state = 'past'
        span.phase = 'ended'
      } else {
        this.setSpan({ ...rec, id, duration_days: 0, ends_day: day, ended_day: day, state: 'past', arrival_agent_id: null, source: 'ended' })
      }
      return
    }
    const arrival = str(p.arrival_agent_id)
    if (span) {
      // The started event is authoritative over a snapshot-merged start; an end already seen is kept.
      const ends = endsDay ?? span.ends_day ?? day + (rec.duration_days ?? 1)
      Object.assign(span, {
        ...rec,
        id,
        ends_day: ends,
        duration_days: ends - day,
        phase: span.ended_day !== null ? 'ended' : phase,
        arrival_agent_id: arrival ?? span.arrival_agent_id,
        source: 'event',
      })
    } else {
      const ends = endsDay ?? day + (rec.duration_days ?? 1)
      this.setSpan({ ...rec, id, ends_day: ends, duration_days: ends - day, ended_day: null, state: 'active', arrival_agent_id: arrival, source: 'event' })
    }
  }

  /** The snapshot's active epoch, recorded when no start was seen (a page loaded mid-epoch); expiry by ends_day. */
  private mergeSnapshotEpochs(snap: SnapshotMsg): void {
    const a: Epoch | null | undefined = snap.epochs?.active
    const id = a ? str(a.id) : null
    if (a && id !== null && !this.epochsById.has(id)) {
      const day = num(a.day) ?? snap.day
      const ends = num(a.ends_day) ?? day + (num(a.duration_days) ?? 1)
      const rec: EpochRecord = {
        seq: -1 - snap.tick,
        tick: snap.tick,
        day,
        kind: String(a.kind),
        phase: 'started',
        duration_days: ends - day,
        scarcity: num(a.scarcity),
        weather_baseline: num(a.weather_baseline),
        id,
        ends_day: ends,
      }
      this.pushEpochRecord(rec)
      this.setSpan({ ...rec, id, ended_day: null, state: 'active', arrival_agent_id: str(a.arrival_agent_id), source: 'snapshot' })
    }
    // the server expires an epoch at the start of its ends_day; the ended event may be outside the backlog
    for (const s of this.epochsById.values()) if (s.state === 'active' && s.ends_day !== null && s.ends_day <= snap.day) s.state = 'past'
  }

  private pushEpochRecord(rec: EpochRecord): void {
    this.epochs.push(rec)
    if (this.epochs.length > MAX_EPOCH_RECORDS) this.epochs.shift()
  }

  private setSpan(span: EpochSpan): void {
    this.epochsById.set(span.id, span)
    if (this.epochsById.size > MAX_EPOCH_SPANS) {
      const oldest = this.epochsById.keys().next()
      if (!oldest.done) this.epochsById.delete(oldest.value)
    }
  }

  /** Epochs by first day, then id. A span still marked active whose ends_day has passed reads as past. */
  epochSpans(currentDay?: number): EpochSpan[] {
    const out = [...this.epochsById.values()].map((s) =>
      currentDay !== undefined && s.state === 'active' && s.ends_day !== null && s.ends_day <= currentDay ? { ...s, state: 'past' as const } : s,
    )
    return out.sort((x, y) => x.day - y.day || (x.id < y.id ? -1 : x.id > y.id ? 1 : 0))
  }
}
