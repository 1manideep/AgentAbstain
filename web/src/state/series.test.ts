import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Epoch, EventAggregatesResponse, EventMsg, HelloMsg, MetricsGroup, MetricsRow, RosterAgent, SnapshotAgent, SnapshotMsg, StressBin } from '../protocol'
import { claimTier, DEGRADATION_BINS, MAX_GENERATIONS, Series } from './series'
import { useStore } from './store'

const roster = new Map<string, RosterAgent>(
  ['a', 'b', 'c'].map((id, i) => [id, { id, name: id.toUpperCase(), tier: i ? 'dim' : 'genius', generation: i, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' }]),
)

function agent(id: string, tick: number, i: number): SnapshotAgent {
  return {
    id,
    x: i,
    y: -i,
    heading: 0,
    stress: 0.3,
    // vary T_eff with the tick so the histogram has several bins in use
    t_eff: tick % 3 === 0 ? null : 0.6 + ((tick * 7 + i * 3) % 14) / 10,
    degenerate: false,
    asleep: false,
    status: 'alive',
    balance_usd: 1 + ((tick + i) % 5) / 4,
    anim: 'idle',
    last_action: null,
    effects: [],
  }
}

function snap(tick: number, opts: { active?: Epoch | null; day?: number; house?: number } = {}): SnapshotMsg {
  const ids = tick % 10 === 0 ? ['a', 'b'] : ['a', 'b', 'c']
  return {
    type: 'snapshot',
    tick,
    day: opts.day ?? Math.floor(tick / 24),
    tick_of_day: tick % 24,
    ts_ms: 1000 + tick * 500,
    paused: false,
    weather: 0,
    scarcity: 1,
    spend_today_usd: 0,
    spend_total_usd: 0,
    spawn_pool_usd: 5,
    house_usd: opts.house,
    population: ids.length,
    gadgets_rev: 0,
    tasks_rev: 0,
    chronicle_rev: 0,
    epochs: { active: opts.active ?? null, scheduled: [] },
    agents: ids.map((id, i) => agent(id, tick, i)),
    nodes: [],
  }
}

let seq = 0
function ev(kind: string, payload: Record<string, unknown>, day = 1, agent_id: string | null = null): EventMsg {
  seq++
  return { type: 'event', seq, tick: day * 24 + 1, day, kind, agent_id, visibility: 'public', payload } as EventMsg
}

function metricsRow(tick: number, byTier: Record<string, Partial<MetricsGroup>>): MetricsRow {
  const by_tier: Record<string, MetricsGroup> = {}
  for (const [t, g] of Object.entries(byTier)) by_tier[t] = { population: 2, ...g }
  return { run_id: 'r', seed: 1, config_hash: 'h', experiment: null, arm: null, tick, day: 0, population: 4, by_tier, by_generation: {} }
}

function bins(over: Record<number, Partial<StressBin>>): StressBin[] {
  return Array.from({ length: DEGRADATION_BINS }, (_, k) => ({ lo: k / 10, calls: 0, coherence_sum: 0, coherence_n: 0, invalid_sum: 0, ...over[k] }))
}

/** Everything the snapshot reducer touches, copied so a later comparison is by value. */
function fingerprint(s: Series) {
  const ticks: number[] = []
  const pops: number[] = []
  for (let i = 0; i < s.popByTick.count; i++) {
    ticks.push(s.popByTick.tickAt(i))
    pops.push(s.popByTick.valueAt(i))
  }
  const gens: number[][] = []
  for (let i = 0; i < s.popByGenCount; i++) gens.push(Array.from(s.popByGenAt(i).byGen))
  return {
    teffSamples: s.teffSamples,
    teffBins: Array.from(s.teffBins),
    popCount: s.popByTick.count,
    ticks,
    pops,
    giniCount: s.giniByTick.count,
    balanceCount: s.meanBalanceByTick.count,
    popByGenCount: s.popByGenCount,
    gens,
    lastSnapshotTick: s.lastSnapshotTick,
    snapshotsCounted: s.snapshotsCounted,
  }
}

describe('backlog-safe snapshots', () => {
  it('ignores the 60 replayed backlog snapshots of a reconnect', () => {
    const s = new Series()
    s.beginRun('run-1')
    const snaps = Array.from({ length: 100 }, (_, t) => snap(t))
    for (const m of snaps) expect(s.onSnapshot(m, roster)).toBe(true)
    const before = fingerprint(s)
    expect(before.popCount).toBe(100)
    expect(before.teffSamples).toBeGreaterThan(0)

    // DESIGN §15: a reconnect to the same run replays the last 60 snapshots
    expect(s.beginRun('run-1')).toBe(false)
    for (const m of snaps.slice(40)) expect(s.onSnapshot(m, roster)).toBe(false)
    expect(fingerprint(s)).toEqual(before)

    // the live stream then continues where it left off
    expect(s.onSnapshot(snap(100), roster)).toBe(true)
    expect(s.popByTick.count).toBe(101)
    expect(s.popByTick.tickAt(100)).toBe(100)
  })

  it('a repeated snapshot of the newest tick is not counted twice', () => {
    const s = new Series()
    s.onSnapshot(snap(5), roster)
    const n = s.teffSamples
    expect(s.onSnapshot(snap(5), roster)).toBe(false)
    expect(s.teffSamples).toBe(n)
    expect(s.popByTick.count).toBe(1)
  })

  it('a new run id clears the aggregates and counts its ticks from the start', () => {
    const s = new Series()
    s.beginRun('run-1')
    for (let t = 0; t < 30; t++) s.onSnapshot(snap(t), roster)
    s.onMetrics(metricsRow(29, {}))
    s.onEvent(ev('maintenance', { agent_id: 'a', real_cost: 10, world_cost: 20, payer: 'a' }), roster)
    expect(s.beginRun('run-2')).toBe(true)
    expect(s.runId).toBe('run-2')
    expect(s.popByTick.count).toBe(0)
    expect(s.teffSamples).toBe(0)
    expect(s.lastMetricsTick).toBe(-1)
    expect(s.memory.byAgent.size).toBe(0)
    expect(s.onSnapshot(snap(0), roster)).toBe(true)
    expect(s.popByTick.count).toBe(1)
  })
})

describe('epochs by id', () => {
  it('a started then ended epoch spans its days and is past after the end', () => {
    const s = new Series()
    s.onEvent(ev('epoch', { kind: 'drought', id: 'e1', phase: 'started', ends_day: 5, scarcity: 0.4, weather_baseline: null, arrival_agent_id: null }, 2), roster)
    const open = s.epochsById.get('e1')!
    expect(open).toMatchObject({ id: 'e1', kind: 'drought', day: 2, ends_day: 5, duration_days: 3, state: 'active', ended_day: null, scarcity: 0.4, source: 'event' })
    expect(s.epochSpans(3).map((e) => e.state)).toEqual(['active'])
    // the event log keeps id and ends_day and derives the duration the server does not send
    expect(s.epochs).toHaveLength(1)
    expect(s.epochs[0]).toMatchObject({ id: 'e1', phase: 'started', day: 2, ends_day: 5, duration_days: 3 })

    s.onEvent(ev('epoch', { kind: 'drought', id: 'e1', phase: 'ended' }, 5), roster)
    const done = s.epochsById.get('e1')!
    expect(s.epochsById.size).toBe(1)
    expect(done).toMatchObject({ day: 2, ends_day: 5, ended_day: 5, state: 'past', phase: 'ended' })
    expect(s.epochSpans(6)).toEqual([done])
    expect(s.epochs.map((e) => [e.id, e.phase])).toEqual([
      ['e1', 'started'],
      ['e1', 'ended'],
    ])
  })

  it('records an epoch already active at page load from the snapshot', () => {
    const s = new Series()
    const active: Epoch = { id: 'op1', kind: 'storm', day: 3, duration_days: 1, ends_day: 7, weather_baseline: -0.5, scarcity: null }
    s.onSnapshot(snap(4 * 24, { active }), roster)
    const span = s.epochsById.get('op1')!
    expect(span).toMatchObject({ id: 'op1', kind: 'storm', day: 3, ends_day: 7, duration_days: 4, state: 'active', source: 'snapshot', weather_baseline: -0.5 })
    // the existing strip reads the log: it gets one started record spanning the epoch
    expect(s.epochs).toHaveLength(1)
    expect(s.epochs[0]).toMatchObject({ id: 'op1', phase: 'started', day: 3, duration_days: 4 })
    expect(s.epochs[0]!.seq).toBeLessThan(0)

    // later snapshots with the same active epoch add nothing
    s.onSnapshot(snap(4 * 24 + 1, { active }), roster)
    expect(s.epochs).toHaveLength(1)
    expect(s.epochsById.size).toBe(1)

    // a backfilled started event replaces the snapshot's stand-in instead of adding a second record
    s.onEvent(ev('epoch', { kind: 'storm', id: 'op1', phase: 'started', ends_day: 7, weather_baseline: -0.5 }, 3), roster)
    expect(s.epochs).toHaveLength(1)
    expect(s.epochs[0]!.seq).toBeGreaterThan(0)
    expect(s.epochsById.get('op1')).toMatchObject({ source: 'event', day: 3, ends_day: 7, state: 'active' })

    s.onEvent(ev('epoch', { kind: 'storm', id: 'op1', phase: 'ended' }, 7), roster)
    expect(s.epochsById.get('op1')).toMatchObject({ state: 'past', ended_day: 7 })
  })

  it('marks a snapshot-merged epoch past once its ends_day is reached without an ended event', () => {
    const s = new Series()
    s.onSnapshot(snap(30, { day: 1, active: { id: 'cfg0', kind: 'boom', day: 1, duration_days: 1, ends_day: 2 } }), roster)
    expect(s.epochsById.get('cfg0')!.state).toBe('active')
    s.onSnapshot(snap(49, { day: 2 }), roster)
    expect(s.epochsById.get('cfg0')!.state).toBe('past')
  })

  it('an ended event whose start was never seen is still recorded', () => {
    const s = new Series()
    s.onEvent(ev('epoch', { kind: 'drought', id: 'cfg3', phase: 'ended' }, 9), roster)
    expect(s.epochsById.get('cfg3')).toMatchObject({ state: 'past', day: 9, ended_day: 9, source: 'ended' })
  })
})

describe('degradation vs per-call stress', () => {
  it('sums stress bins weighted by calls, per tier, ignoring replayed rows', () => {
    const s = new Series()
    s.onMetrics(
      metricsRow(10, {
        genius: { stress_bins: bins({ 1: { calls: 3, coherence_sum: 2.4, coherence_n: 3, invalid_sum: 0 }, 9: { calls: 1, coherence_sum: 0.1, coherence_n: 1, invalid_sum: 1 } }) },
        dim: { stress_bins: bins({ 5: { calls: 2, coherence_sum: 0.5, coherence_n: 1, invalid_sum: 1 } }) },
      }),
    )
    s.onMetrics(metricsRow(11, { genius: { stress_bins: bins({ 1: { calls: 1, coherence_sum: 0.2, coherence_n: 1, invalid_sum: 1 } }) } }))
    // a replayed row changes nothing
    s.onMetrics(metricsRow(11, { genius: { stress_bins: bins({ 1: { calls: 50, coherence_sum: 0, coherence_n: 50, invalid_sum: 50 } }) } }))

    const g = s.degradationBins.get('genius')!
    expect(g).toHaveLength(DEGRADATION_BINS)
    expect(g[1]).toEqual({ calls: 4, coherence_sum: 2.6, coherence_n: 4, invalid_sum: 1 })
    // call-weighted: (2.4 + 0.2) / 4 = 0.65, not the mean of the two rows' means (0.8 + 0.2) / 2 = 0.5
    const b1 = s.degradationAt('genius', 1)
    expect(b1.calls).toBe(4)
    expect(b1.coherence).toBeCloseTo(0.65, 10)
    expect(b1.invalidRate).toBeCloseTo(0.25, 10)
    expect(s.degradationAt('genius', 9)).toEqual({ calls: 1, coherence: 0.1, invalidRate: 1 })
    expect(s.degradationAt('genius', 4)).toEqual({ calls: 0, coherence: null, invalidRate: null })
    // coherence counts only scored calls; the invalid rate counts every call
    expect(s.degradationAt('dim', 5)).toEqual({ calls: 2, coherence: 0.5, invalidRate: 0.5 })
    expect(s.degradationAt('nobody', 0)).toEqual({ calls: 0, coherence: null, invalidRate: null })
    // the old mean-stress structure is still fed for the current chart
    expect(s.coherenceByTierByStressBin.has('genius')).toBe(true)
  })

  it('places bins by lo and tolerates rows without stress_bins', () => {
    const s = new Series()
    s.onMetrics(metricsRow(1, { genius: {} }))
    expect(s.degradationBins.size).toBe(0)
    s.onMetrics(metricsRow(2, { genius: { stress_bins: [{ lo: 0.7, calls: 2, coherence_sum: 1, coherence_n: 2, invalid_sum: 0 }] } }))
    expect(s.degradationAt('genius', 7)).toEqual({ calls: 2, coherence: 0.5, invalidRate: 0 })
    expect(s.degradationAt('genius', 0).calls).toBe(0)
  })
})

describe('memory accumulators', () => {
  it('sums maintenance spend per agent in dollars and counts skipped maintenance per day', () => {
    const s = new Series()
    s.onEvent(ev('maintenance', { agent_id: 'a', payer: 'a', real_cost: 1500, world_cost: 3000, commands: 2, ok: 2 }, 1, 'a'), roster)
    s.onEvent(ev('maintenance', { agent_id: 'a', payer: 'research_pool', real_cost: 500, world_cost: 1000 }, 2, 'a'), roster)
    s.onEvent(ev('maintenance', { agent_id: 'b', payer: 'b', real_cost: 250, world_cost: 400 }, 2, 'b'), roster)
    s.onEvent(ev('maintenance_skipped', { agent_id: 'c', reason: 'insufficient', payer: 'c' }, 2, 'c'), roster)
    s.onEvent(ev('maintenance_skipped', { agent_id: 'b', reason: 'daily_cap', payer: 'b' }, 2, 'b'), roster)
    s.onEvent(ev('maintenance_skipped', { agent_id: 'c', reason: 'insufficient', payer: 'c' }, 3, 'c'), roster)

    const a = s.memory.byAgent.get('a')!
    expect(a.maintenanceReal).toBeCloseTo(0.002, 12)
    expect(a.maintenanceWorld).toBeCloseTo(0.004, 12)
    expect(a.maintenanceWorldSelf).toBeCloseTo(0.003, 12)
    expect(a.maintenanceCalls).toBe(2)
    expect(s.memory.byAgent.get('b')).toMatchObject({ maintenanceCalls: 1, maintenanceSkipped: 1 })
    expect(s.memory.byAgent.get('b')!.maintenanceReal).toBeCloseTo(0.00025, 12)
    expect(s.memory.byAgent.get('c')).toMatchObject({ maintenanceCalls: 0, maintenanceSkipped: 2 })
    expect(s.memory.maintenanceCalls).toBe(3)
    expect(s.memory.maintenanceReal).toBeCloseTo(0.00225, 12)
    expect([...s.memory.skippedByDay]).toEqual([
      [2, 2],
      [3, 1],
    ])
    expect(s.memory.maintenanceSkipped).toBe(3)
  })

  it('tallies exam answers per agent and per kind per day, keeping failed calls out of asked', () => {
    const s = new Series()
    const answer = (agent: string, kind: string, day: number, correct: boolean, abstained: boolean, failed = false) =>
      s.onEvent(ev('exam_answered', { agent_id: agent, kind, correct, abstained, answer: abstained ? null : 'x', ...(failed ? { failed: true } : {}) }, day, agent), roster)
    answer('a', 'node_stock', 1, true, false)
    answer('a', 'node_stock', 1, false, true)
    answer('a', 'agent_seen', 1, false, false)
    answer('b', 'node_stock', 1, true, false)
    answer('b', 'node_stock', 2, false, true)
    answer('b', 'node_stock', 2, false, true, true)

    expect(s.memory.byAgent.get('a')).toMatchObject({ asked: 3, correct: 1, abstained: 1, failed: 0 })
    expect(s.memory.byAgent.get('b')).toMatchObject({ asked: 2, correct: 1, abstained: 1, failed: 1 })
    expect(s.memory.examByDay.get(1)!.get('node_stock')).toEqual({ asked: 3, correct: 2, abstained: 1, failed: 0 })
    expect(s.memory.examByDay.get(1)!.get('agent_seen')).toEqual({ asked: 1, correct: 0, abstained: 0, failed: 0 })
    expect(s.memory.examByDay.get(2)!.get('node_stock')).toEqual({ asked: 1, correct: 0, abstained: 1, failed: 1 })
  })

  it('falls back to the event agent_id and ignores malformed costs', () => {
    const s = new Series()
    s.onEvent(ev('maintenance', { real_cost: 'lots', world_cost: null }, 1, 'c'), roster)
    expect(s.memory.byAgent.get('c')).toMatchObject({ maintenanceCalls: 1, maintenanceReal: 0, maintenanceWorld: 0 })
  })
})

describe('seeded event aggregates', () => {
  const empty = (asOf: number): EventAggregatesResponse => ({
    as_of_seq: asOf,
    degeneration: { by_generation: [], t_c_by_tier: [] },
    claims: [],
    gossip: [],
    memory: { agents: [], skipped_by_day: [], exams: [] },
    epochs: [],
    gate: [],
  })

  it('files a claim under the roster tier, else the payload tier, else unknown', () => {
    expect(claimTier('a', 'dim', roster)).toBe('genius')
    expect(claimTier('zed', 'slow', roster)).toBe('slow')
    expect(claimTier(7, '', roster)).toBe('unknown')
  })

  it('replaces the totals, skips the events the seed covers and counts the later ones once', () => {
    const s = new Series()
    s.onEvent(ev('claim', { agent_id: 'a', truthful: false, tier: 'genius' }), roster)
    const covered = ev('gossip_transfer', { speaker_id: 'a', listener_id: 'b' })
    const agg = empty(covered.seq)
    agg.claims = [
      { agent_id: 'a', tier: 'genius', made: 5, falsy: 2 },
      { agent_id: 'zed', tier: 'slow', made: 1, falsy: 1 },
      { agent_id: null, tier: null, made: 1, falsy: 0 },
    ]
    agg.degeneration = { by_generation: [{ generation: null, count: 3 }, { generation: 0, count: 2 }, { generation: 40, count: 1 }], t_c_by_tier: [{ tier: 'dim', t_c: 1.8 }] }
    agg.gossip = [{ speaker_id: 'a', listener_id: 'b', count: 4, last_tick: 9 }]
    agg.memory = {
      agents: [{ agent_id: 'b', maintenance_real_micro: 1500, maintenance_world_micro: 3000, maintenance_world_self_micro: 3000, maintenance_calls: 2, maintenance_skipped: 1, asked: 3, correct: 1, abstained: 1, failed: 1 }],
      skipped_by_day: [{ day: 2, count: 2 }],
      exams: [{ day: 1, kind: 'node', asked: 3, correct: 1, abstained: 1, failed: 1 }],
    }
    expect(s.seedEvents(agg, roster)).toBe(true)
    expect([...s.falseClaimByTier]).toEqual([
      ['genius', { made: 5, falsy: 2 }],
      ['slow', { made: 1, falsy: 1 }],
      ['unknown', { made: 1, falsy: 0 }],
    ])
    expect(s.degenTotal).toBe(6)
    expect(s.degenByGen[0]).toBe(2)
    expect(s.degenByGen[MAX_GENERATIONS - 1]).toBe(1)
    expect([...s.tCByTier]).toEqual([['dim', 1.8]])
    expect(s.memory.byAgent.get('b')).toMatchObject({ maintenanceReal: 0.0015, maintenanceWorldSelf: 0.003, maintenanceCalls: 2, asked: 3 })
    expect(s.memory.maintenanceCalls).toBe(2)
    expect(s.memory.maintenanceSkipped).toBe(2)
    expect(s.memory.examByDay.get(1)!.get('node')).toEqual({ asked: 3, correct: 1, abstained: 1, failed: 1 })

    // the seed covers this one: skipped; a later one counts once
    s.onEvent(covered, roster)
    expect(s.gossipTotal).toBe(4)
    s.onEvent(ev('gossip_transfer', { speaker_id: 'a', listener_id: 'b' }), roster)
    expect(s.gossipTotal).toBe(5)
    expect([...s.gossipEdges.values()]).toEqual([{ from: 'a', to: 'b', count: 5, lastTick: 25 }])
    s.onEvent(ev('maintenance', { agent_id: 'b', payer: 'b', real_cost: 500, world_cost: 1000 }, 3, 'b'), roster)
    expect(s.memory.byAgent.get('b')!.maintenanceReal).toBe(0.002)
    expect(s.memory.maintenanceReal).toBe(0.002)
  })

  it('refuses a seed older than the events it has counted; clear starts over', () => {
    const s = new Series()
    const e = ev('claim', { agent_id: 'a', truthful: true, tier: 'genius' })
    s.onEvent(e, roster)
    expect(s.seedEvents(empty(e.seq - 1), roster)).toBe(false)
    expect(s.falseClaimByTier.get('genius')).toEqual({ made: 1, falsy: 0 })
    expect(s.seedEvents(empty(e.seq), roster)).toBe(true)
    expect(s.falseClaimByTier.size).toBe(0)
    s.clear()
    expect([s.aggregatesSeq, s.countedSeq]).toEqual([0, 0])
  })
})

describe('store', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  function hello(run_id: string, tick_seconds: number, configTickSeconds: number): HelloMsg {
    return {
      type: 'hello',
      run_id,
      tick: 0,
      day: 0,
      last_seq: 0,
      paused: false,
      tick_seconds,
      server_ts_ms: 0,
      status: 'running',
      config: {
        name: 'test',
        seed: 1,
        days: 2,
        ticks_per_day: 24,
        tick_seconds: configTickSeconds,
        render_delay_ticks: 2,
        world_size: 100,
        population_cap: 10,
        daily_cap_usd: 1,
        total_cap_usd: 2,
        tiers: {},
      },
    }
  }

  it('takes the live tick_seconds from hello, house_usd from snapshots, and drops replayed snapshots from the series', () => {
    vi.useFakeTimers()
    const st = useStore.getState()
    st.applyHello(hello('run-a', 0.25, 2))
    expect(useStore.getState().tickSeconds).toBe(0.25)
    expect(useStore.getState().series.runId).toBe('run-a')

    for (let t = 0; t < 5; t++) st.applySnapshot(snap(t, { house: 1.5 + t }))
    vi.advanceTimersByTime(1000)
    expect(useStore.getState().world.houseUsd).toBe(5.5)
    expect(useStore.getState().series.popByTick.count).toBe(5)

    // reconnect to the same run: the replayed backlog does not reach the series
    st.applyHello(hello('run-a', 0.5, 2))
    for (let t = 0; t < 5; t++) st.applySnapshot(snap(t))
    expect(useStore.getState().series.popByTick.count).toBe(5)
    expect(useStore.getState().tickSeconds).toBe(0.5)

    // a different run resets everything
    st.applyHello(hello('run-b', 1, 1))
    expect(useStore.getState().series.runId).toBe('run-b')
    expect(useStore.getState().series.popByTick.count).toBe(0)
    st.applySnapshot(snap(0))
    vi.advanceTimersByTime(1000)
    expect(useStore.getState().series.popByTick.count).toBe(1)
    // a snapshot without house_usd (older server) reads as 0
    expect(useStore.getState().world.houseUsd).toBe(0)
  })
})
