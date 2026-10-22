/**
 * A fresh tab on a run with more events than the 2000-event ring holds shows the same event-derived chart
 * aggregates as a tab that ingested every event live (DESIGN §15 GET /api/events/aggregates), with no double count
 * at the seam between the seed and the live stream, and the event feed and the scrub history are what they were
 * without the seed.
 *
 * The fake server answers /api/events/aggregates with `aggregate` below, which folds the events the way
 * void/server/api.py's SQL does (tests/test_server_event_aggregates.py holds the SQL to the same rules); the
 * "watched" client is a Series and an EventRing that see every event and snapshot in order, as a tab open since
 * tick 0 does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ClaimTally,
  EventAggregatesResponse,
  EventMsg,
  ExamDayTally,
  GossipEdgeTally,
  HelloMsg,
  MemoryAgentTally,
  RosterAgent,
  ServerMsg,
  SnapshotAgent,
  SnapshotMsg,
} from '../protocol'
import { EventRing } from '../state/events'
import { history } from '../state/history'
import { MAX_GATE_EVENTS, Series } from '../state/series'
import { useStore } from '../state/store'
import { WsClient } from './ws'

const mocks = vi.hoisted(() => ({ apiGet: vi.fn() }))

vi.mock('./api', () => ({
  apiGet: mocks.apiGet,
  ensureToken: () => Promise.resolve(null),
  FIXTURE_MODE: false,
}))
vi.mock('./fixture', () => ({ startFixture: () => Promise.resolve(() => {}) }))

// ------------------------------------------------------------------ a synthetic run

const TICKS_PER_DAY = 24
const EVENTS_PER_TICK = 20
/** The tick the fresh tab connects at (hello.tick); hello.last_seq is the last event of that tick. */
const HELLO_TICK = 300
/** The server has run this far by the time the fresh tab's GETs are answered. */
const AHEAD_TICK = 302
const LAST_TICK = 340
const TIERS = ['genius', 'sharp', 'average', 'slow', 'dim']

const ROSTER: RosterAgent[] = Array.from({ length: 8 }, (_, i) => ({
  id: `a${i}`,
  name: `Agent${i}`,
  tier: TIERS[i % TIERS.length]!,
  generation: i % 3,
  parent_id: null,
  born_tick: 0,
  died_tick: null,
  status: 'alive',
}))
const ROSTER_BY_ID = new Map(ROSTER.map((a) => [a.id, a]))

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const dayOf = (tick: number) => Math.floor(tick / TICKS_PER_DAY)

/** Epoch events at fixed seqs: cfg0 runs days 0-1 and ends; op1 starts before the hello and ends after it. */
const EPOCH_AT: Record<number, (tick: number) => Record<string, unknown>> = {
  50: (t) => ({ kind: 'drought', id: 'cfg0', phase: 'started', ends_day: dayOf(t) + 2, scarcity: 0.5, weather_baseline: -0.5 }),
  1500: () => ({ kind: 'drought', id: 'cfg0', phase: 'ended' }),
  5900: (t) => ({ kind: 'boom', id: 'op1', phase: 'started', ends_day: dayOf(t) + 2, scarcity: 1.6, weather_baseline: 0.4 }),
  6500: () => ({ kind: 'boom', id: 'op1', phase: 'ended' }),
}

/** Every event of the run, seq 1.., EVENTS_PER_TICK per tick, with the payload variations the reducers branch on. */
function makeEvents(): EventMsg[] {
  const rand = mulberry32(20261022)
  const pick = <T>(xs: readonly T[]): T => xs[Math.floor(rand() * xs.length)]!
  const out: EventMsg[] = []
  for (let tick = 1; tick <= LAST_TICK; tick++) {
    for (let i = 0; i < EVENTS_PER_TICK; i++) {
      const seq = out.length + 1
      const day = dayOf(tick)
      const who = pick(ROSTER)
      const ev = (kind: string, payload: Record<string, unknown>, agentId: string | null = who.id, visibility = 'public'): EventMsg =>
        ({ type: 'event', seq, tick, day, kind, agent_id: agentId, visibility, payload }) as unknown as EventMsg
      const epoch = EPOCH_AT[seq]
      if (epoch) {
        out.push(ev('epoch', epoch(tick), null))
        continue
      }
      const r = rand()
      if (r < 0.3) out.push(ev('move', {}))
      else if (r < 0.48) {
        // a speaker missing from the roster files under its payload tier; truthful may be absent (counts as false)
        const ghost = rand() < 0.05
        const truthful = pick([true, true, true, false, undefined])
        out.push(ev('claim', { agent_id: ghost ? 'ghost' : who.id, claim: {}, truthful, tier: ghost ? 'dim' : who.tier, listener_id: null }))
      } else if (r < 0.56) {
        const to = pick(ROSTER.filter((a) => a.id !== who.id))
        out.push(ev('gossip_transfer', { speaker_id: who.id, listener_id: to.id, hop: 1 }, to.id))
      } else if (r < 0.62) {
        // generations past the chart's last bar clamp into it; a non-integral one counts in the total only
        const generation = pick([0, 0, 1, 1, 2, 3, 15, -1, 2.5, undefined])
        out.push(ev('degeneration', { mode: 'induced', t_eff: 2, t_c: pick([1.8, 1.9]), tier: rand() < 0.1 ? '' : who.tier, generation, agent_name: who.name }))
      } else if (r < 0.68) {
        const payer = rand() < 0.5 ? who.id : 'research_pool'
        out.push(ev('maintenance', { agent_id: who.id, payer, real_cost: 100 + Math.floor(rand() * 5000), world_cost: 50 + Math.floor(rand() * 900), commands: 2 }))
      } else if (r < 0.71) {
        out.push(rand() < 0.3 ? ev('maintenance_skipped', { reason: 'broke' }, null) : ev('maintenance_skipped', { agent_id: who.id, reason: 'broke' }))
      } else if (r < 0.79) {
        const failed = rand() < 0.1
        out.push(ev('exam_answered', { agent_id: who.id, kind: pick(['balance', 'node', 'claim', '']), correct: rand() < 0.5, abstained: rand() < 0.2, ...(failed ? { failed: true } : {}) }, who.id, 'operator'))
      } else if (r < 0.85) {
        const verified = rand() < 0.6
        out.push(ev(verified ? 'gadget_verified' : 'gadget_rejected', { gadget_id: `g${seq}`, name: `gadget-${seq}`, owner: who.id, stage: verified ? 'verified' : pick(['tests', 'static', 'size']), reason: verified ? '' : 'no' }))
      } else out.push(ev('talk', { speaker_id: who.id, listener_id: pick(ROSTER).id, text: 'hello', claims_made: 0, claims_false: 0 }))
    }
  }
  return out
}

const EVENTS = makeEvents()
const lastSeqOf = (tick: number) => tick * EVENTS_PER_TICK
const HELLO_SEQ = lastSeqOf(HELLO_TICK)

function agent(a: RosterAgent, tick: number): SnapshotAgent {
  return {
    id: a.id,
    x: (tick % 10) + Number(a.id.slice(1)),
    y: 0,
    heading: 0,
    stress: 0.2,
    t_eff: 0.9,
    degenerate: false,
    asleep: false,
    status: 'alive',
    balance_usd: 1.5,
    anim: 'idle',
    last_action: null,
    effects: [],
  }
}

function snapshot(tick: number): SnapshotMsg {
  const day = dayOf(tick)
  const op1 = EVENTS.find((e) => e.seq === 5900)!
  const active = tick >= op1.tick && tick < 6500 / EVENTS_PER_TICK ? { id: 'op1', kind: 'boom', day: op1.day, ends_day: op1.day + 2, duration_days: 2 } : null
  return {
    type: 'snapshot',
    tick,
    day,
    tick_of_day: tick % TICKS_PER_DAY,
    ts_ms: 1_000_000 + tick * 500,
    paused: false,
    weather: 0,
    scarcity: 1,
    spend_today_usd: 0,
    spend_total_usd: 0,
    spawn_pool_usd: 5,
    population: ROSTER.length,
    gadgets_rev: 0,
    tasks_rev: 0,
    chronicle_rev: 0,
    epochs: { active, scheduled: [] },
    agents: ROSTER.map((a) => agent(a, tick)),
    nodes: [{ id: 'n1', x: 10, y: 10, stock: 20, capacity: 40, stock_delta: 0 }],
  } as SnapshotMsg
}

// ------------------------------------------------------------------ the server's aggregate

const isStr = (v: unknown): v is string => typeof v === 'string'
const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v !== ''
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** GET /api/events/aggregates over the events with seq <= asOf, folded the way the route's SQL folds them. */
function aggregate(events: readonly EventMsg[], asOf: number): EventAggregatesResponse {
  const byGen = new Map<number | null, number>()
  const tC = new Map<string, number>()
  const claims = new Map<string, ClaimTally>()
  const gossip = new Map<string, GossipEdgeTally>()
  const agents = new Map<string, MemoryAgentTally>()
  const skipped = new Map<number, number>()
  const exams = new Map<string, ExamDayTally>()
  const epochs: EventMsg[] = []
  const gate: EventMsg[] = []
  const memAgent = (id: string): MemoryAgentTally => {
    let a = agents.get(id)
    if (!a) {
      a = { agent_id: id, maintenance_real_micro: 0, maintenance_world_micro: 0, maintenance_world_self_micro: 0, maintenance_calls: 0, maintenance_skipped: 0, asked: 0, correct: 0, abstained: 0, failed: 0 }
      agents.set(id, a)
    }
    return a
  }
  let upto = 0
  for (const e of events) {
    if (e.seq > asOf) break
    upto = e.seq
    const p = e.payload as Record<string, unknown>
    const aid = nonEmpty(p.agent_id) ? p.agent_id : e.agent_id
    if (e.kind === 'degeneration') {
      const g = typeof p.generation === 'number' && Number.isInteger(p.generation) ? p.generation : null
      byGen.set(g, (byGen.get(g) ?? 0) + 1)
      if (nonEmpty(p.tier) && finite(p.t_c)) tC.set(p.tier, p.t_c)
    } else if (e.kind === 'claim') {
      const id = isStr(p.agent_id) ? p.agent_id : null
      const tier = nonEmpty(p.tier) ? p.tier : null
      const key = JSON.stringify([id, tier])
      const c = claims.get(key) ?? { agent_id: id, tier, made: 0, falsy: 0 }
      c.made++
      if (!p.truthful) c.falsy++
      claims.set(key, c)
    } else if (e.kind === 'gossip_transfer') {
      const from = isStr(p.speaker_id) ? p.speaker_id : ''
      const to = isStr(p.listener_id) ? p.listener_id : ''
      const key = JSON.stringify([from, to])
      const g = gossip.get(key) ?? { speaker_id: from, listener_id: to, count: 0, last_tick: e.tick }
      g.count++
      g.last_tick = Math.max(g.last_tick, e.tick)
      gossip.set(key, g)
    } else if (e.kind === 'maintenance') {
      if (!aid) continue
      const a = memAgent(aid)
      const real = finite(p.real_cost) ? p.real_cost : 0
      const world = finite(p.world_cost) ? p.world_cost : 0
      a.maintenance_real_micro += real
      a.maintenance_world_micro += world
      if (p.payer === aid) a.maintenance_world_self_micro += world
      a.maintenance_calls++
    } else if (e.kind === 'maintenance_skipped') {
      skipped.set(e.day, (skipped.get(e.day) ?? 0) + 1)
      if (aid) memAgent(aid).maintenance_skipped++
    } else if (e.kind === 'exam_answered') {
      const kind = nonEmpty(p.kind) ? p.kind : 'unknown'
      const key = JSON.stringify([e.day, kind])
      const x = exams.get(key) ?? { day: e.day, kind, asked: 0, correct: 0, abstained: 0, failed: 0 }
      exams.set(key, x)
      const a = aid ? memAgent(aid) : null
      if (p.failed === true) {
        x.failed++
        if (a) a.failed++
        continue
      }
      x.asked++
      if (a) a.asked++
      if (p.correct === true) {
        x.correct++
        if (a) a.correct++
      }
      if (p.abstained === true) {
        x.abstained++
        if (a) a.abstained++
      }
    } else if (e.kind === 'epoch') epochs.push(e)
    else if (e.kind === 'gadget_verified' || e.kind === 'gadget_rejected') gate.push(e)
  }
  return {
    as_of_seq: upto,
    degeneration: { by_generation: [...byGen].map(([generation, count]) => ({ generation, count })), t_c_by_tier: [...tC].map(([tier, t_c]) => ({ tier, t_c })) },
    claims: [...claims.values()],
    gossip: [...gossip.values()],
    memory: { agents: [...agents.values()], skipped_by_day: [...skipped].map(([day, count]) => ({ day, count })), exams: [...exams.values()] },
    epochs,
    gate: gate.slice(-MAX_GATE_EVENTS),
  }
}

// ------------------------------------------------------------------ fake server and socket

class FakeSocket {
  static all: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    FakeSocket.all.push(this)
  }
  close(): void {}
  send(msg: ServerMsg): void {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
}

/** How far the run has got when a GET is answered, and whether the server has the aggregates route. */
const server = { tick: AHEAD_TICK, aggregates: true }

function answer(path: string): unknown {
  const u = new URL(path, 'http://void.test')
  const q = (k: string, d: number) => (u.searchParams.has(k) ? Number(u.searchParams.get(k)) : d)
  const maxSeq = lastSeqOf(server.tick)
  if (u.pathname === '/api/events') {
    const since = q('since_seq', 0)
    const limit = q('limit', 500)
    return { events: EVENTS.filter((e) => e.seq > since && e.seq <= maxSeq).slice(0, limit), last_seq: maxSeq }
  }
  if (u.pathname === '/api/events/aggregates' && server.aggregates) {
    return aggregate(EVENTS, Math.min(q('as_of_seq', maxSeq), maxSeq))
  }
  if (u.pathname === '/api/metrics') return u.searchParams.get('days') ? { days: [] } : { metrics: [] }
  if (u.pathname === '/api/snapshots') {
    const since = q('since_tick', 0)
    const limit = q('limit', 60)
    const out: SnapshotMsg[] = []
    for (let t = Math.max(1, since + 1); t <= server.tick && out.length < limit; t++) out.push(snapshot(t))
    return { snapshots: out }
  }
  throw new Error(`404 ${path}`)
}

function hello(): HelloMsg {
  return {
    type: 'hello',
    run_id: 'agg-run',
    config: { render_delay_ticks: 2, population_cap: 16, ticks_per_day: TICKS_PER_DAY, tick_seconds: 0.5, tiers: {} },
    tick: HELLO_TICK,
    day: dayOf(HELLO_TICK),
    last_seq: HELLO_SEQ,
    paused: false,
    tick_seconds: 0.5,
    server_ts_ms: 0,
    status: 'running',
  } as unknown as HelloMsg
}

/** Send one tick the way the hub does: its events, then its snapshot. */
function sendTick(ws: FakeSocket, tick: number): void {
  for (const e of EVENTS) if (e.tick === tick) ws.send(e)
  ws.send(snapshot(tick))
}

// ------------------------------------------------------------------ the two clients

/** A tab open since tick 0: every event and snapshot, in order. */
class Watched {
  series = new Series()
  ring = new EventRing(2000)
  tick = 0
  until(tick: number): this {
    for (; this.tick < tick; ) {
      this.tick++
      for (const e of EVENTS) if (e.tick === this.tick && this.ring.push(e)) this.series.onEvent(e, ROSTER_BY_ID)
      this.series.onSnapshot(snapshot(this.tick), ROSTER_BY_ID)
    }
    return this
  }
}

/** Everything the event-derived charts read, in the order they read it. */
function charts(s: Series) {
  return {
    degenByGen: Array.from(s.degenByGen),
    degenTotal: s.degenTotal,
    tCByTier: [...s.tCByTier],
    falseClaimByTier: [...s.falseClaimByTier].map(([tier, c]) => [tier, c.made, c.falsy]),
    gossipEdges: [...s.gossipEdges.values()].map((e) => [e.from, e.to, e.count, e.lastTick]),
    gossipTotal: s.gossipTotal,
    epochs: s.epochs.map((r) => [r.seq, r.id, r.phase, r.day, r.ends_day, r.duration_days]),
    epochSpans: s.epochSpans().map((x) => [x.id, x.state, x.phase, x.day, x.ends_day, x.ended_day, x.source]),
    memoryAgents: [...s.memory.byAgent],
    skippedByDay: [...s.memory.skippedByDay],
    examByDay: [...s.memory.examByDay].map(([day, byKind]) => [day, [...byKind]]),
    memoryTotals: [s.memory.maintenanceReal, s.memory.maintenanceWorld, s.memory.maintenanceCalls, s.memory.maintenanceSkipped],
    gate: s.gateEvents.map((e) => e.seq),
  }
}

const ringSeqs = (ring: EventRing): number[] => {
  const out: number[] = []
  ring.forEach((e) => out.push(e.seq))
  return out
}

function historyTicks(): number[] {
  const out: number[] = []
  for (let i = 0; i < history.count; i++) out.push(history.frameAt(i).tick)
  return out
}

let client: WsClient | null = null

/** A fresh tab joins at HELLO_TICK while the server runs ahead to AHEAD_TICK; returns its socket once settled. */
async function freshTab(): Promise<FakeSocket> {
  client = new WsClient()
  client.start()
  const ws = FakeSocket.all[FakeSocket.all.length - 1]!
  ws.onopen?.()
  ws.send(hello())
  ws.send({ type: 'roster', agents: ROSTER })
  for (let t = HELLO_TICK - 59; t <= HELLO_TICK; t++) ws.send(snapshot(t)) // the preamble's 60-snapshot backlog
  for (let t = HELLO_TICK + 1; t <= AHEAD_TICK; t++) sendTick(ws, t) // live while the backfill runs
  await client.settled()
  return ws
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('location', { protocol: 'http:', host: 'void.test' })
  FakeSocket.all = []
  server.tick = AHEAD_TICK
  server.aggregates = true
  mocks.apiGet.mockReset()
  mocks.apiGet.mockImplementation((path: string) => {
    try {
      return Promise.resolve(answer(path))
    } catch (err) {
      return Promise.reject(err)
    }
  })
  history.reset()
  history.runId = null
  useStore.getState().resetRun()
  useStore.setState({ runId: null })
})

afterEach(() => {
  client?.stop()
  client = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// ------------------------------------------------------------------ tests

describe('event-derived aggregates on a fresh tab', () => {
  it('the synthetic run is longer than the event ring and exercises every chart', () => {
    expect(HELLO_SEQ).toBeGreaterThan(2 * 2000)
    const kinds = new Set(EVENTS.filter((e) => e.seq <= HELLO_SEQ - 2000).map((e) => e.kind))
    // every aggregated kind also occurs before the ring's window, so a ring-only tab would undercount each one
    for (const k of ['degeneration', 'claim', 'gossip_transfer', 'epoch', 'gadget_verified', 'gadget_rejected', 'maintenance', 'maintenance_skipped', 'exam_answered']) {
      expect(kinds.has(k), k).toBe(true)
    }
    expect(EVENTS.filter((e) => e.kind.startsWith('gadget_') && e.seq <= HELLO_SEQ).length).toBeGreaterThan(MAX_GATE_EVENTS)
  })

  it('equal a client that ingested every event live, at the seam and after further live events', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ws = await freshTab()
    expect(mocks.apiGet.mock.calls.map((c) => c[0])).toContain(`/api/events/aggregates?as_of_seq=${HELLO_SEQ}`)
    const s = useStore.getState()
    expect(s.series.aggregatesSeq).toBe(HELLO_SEQ)

    const watched = new Watched().until(AHEAD_TICK)
    expect(charts(s.series)).toEqual(charts(watched.series))
    // the feed holds the newest 2000 events, as the watched tab's ring does
    expect(ringSeqs(s.events)).toEqual(ringSeqs(watched.ring))
    expect(ringSeqs(s.events)).toEqual(range(lastSeqOf(AHEAD_TICK) - 1999, lastSeqOf(AHEAD_TICK)))

    // the run goes on: every later event counts once, including a replayed one
    for (let t = AHEAD_TICK + 1; t <= LAST_TICK; t++) {
      sendTick(ws, t)
      if (t === AHEAD_TICK + 5) ws.send(EVENTS[lastSeqOf(t) - 1]!)
    }
    watched.until(LAST_TICK)
    expect(charts(s.series)).toEqual(charts(watched.series))
    expect(ringSeqs(s.events)).toEqual(ringSeqs(watched.ring))
    // the op1 epoch started before the ring's window and ended live: its span is complete
    expect(s.series.epochSpans().map((x) => [x.id, x.state, x.source])).toEqual([
      ['cfg0', 'past', 'event'],
      ['op1', 'past', 'event'],
    ])
    warn.mockRestore()
  })

  it('without the route the feed and scrub history are unchanged, and only the charts fall back to the ring', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await freshTab()
    const seeded = { ring: ringSeqs(useStore.getState().events), frames: historyTicks(), charts: charts(useStore.getState().series) }
    client!.stop()
    client = null

    history.reset()
    history.runId = null
    useStore.getState().resetRun()
    useStore.setState({ runId: null })
    server.aggregates = false
    await freshTab()
    const s = useStore.getState()
    expect(s.series.aggregatesSeq).toBe(0)
    // the feed and the scrub history do not depend on the seed
    expect(ringSeqs(s.events)).toEqual(seeded.ring)
    expect(historyTicks()).toEqual(seeded.frames)
    expect(historyTicks()).toEqual(range(AHEAD_TICK - history.ringCapacity + 1, AHEAD_TICK).filter((t) => t >= 1))
    // the ring-only totals are what the fix is about: smaller than the whole run's
    const ringOnly = charts(s.series)
    expect(ringOnly.degenTotal).toBeLessThan(seeded.charts.degenTotal)
    expect(ringOnly.gossipTotal).toBeLessThan(seeded.charts.gossipTotal)
    const made = (c: ReturnType<typeof charts>) => c.falseClaimByTier.reduce((n, r) => n + (r[1] as number), 0)
    expect(made(ringOnly)).toBeLessThan(made(seeded.charts))
    warn.mockRestore()
  })

  it('a reconnect that missed more events than it pages is made whole by the seed', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ws = await freshTab()
    // the socket drops at AHEAD_TICK; the run reaches LAST_TICK before the next hello
    ws.onclose?.()
    server.tick = LAST_TICK
    vi.advanceTimersByTime(6000)
    const ws2 = FakeSocket.all[FakeSocket.all.length - 1]!
    expect(ws2).not.toBe(ws)
    ws2.onopen?.()
    // a catch-up of one page only: the seed has to cover the rest
    mocks.apiGet.mockImplementation((path: string) => {
      try {
        const body = answer(path) as { events?: EventMsg[] }
        if (path.startsWith('/api/events?')) return Promise.resolve({ ...body, events: (body.events ?? []).slice(0, 100) })
        return Promise.resolve(body)
      } catch (err) {
        return Promise.reject(err)
      }
    })
    ws2.send({ ...hello(), tick: LAST_TICK, day: dayOf(LAST_TICK), last_seq: lastSeqOf(LAST_TICK) })
    await client!.settled()
    const s = useStore.getState()
    expect(s.series.aggregatesSeq).toBe(lastSeqOf(LAST_TICK))
    const watched = new Watched().until(LAST_TICK)
    const got = charts(s.series)
    const want = charts(watched.series)
    expect(got.degenTotal).toBe(want.degenTotal)
    expect(got.falseClaimByTier).toEqual(want.falseClaimByTier)
    expect(got.gossipEdges).toEqual(want.gossipEdges)
    expect(got.memoryAgents).toEqual(want.memoryAgents)
    expect(got.gate).toEqual(want.gate)
    warn.mockRestore()
  })
})

function range(from: number, to: number): number[] {
  const out: number[] = []
  for (let i = from; i <= to; i++) out.push(i)
  return out
}
