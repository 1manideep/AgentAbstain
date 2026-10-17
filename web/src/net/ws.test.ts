import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DayMsg, EventMsg, HelloMsg, MetricsMsg, ServerMsg, SnapshotMsg } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { WsClient } from './ws'

const mocks = vi.hoisted(() => ({ apiGet: vi.fn(), ingest: vi.fn() }))

vi.mock('./api', () => ({
  apiGet: mocks.apiGet,
  ensureToken: () => Promise.resolve(null),
  FIXTURE_MODE: false,
}))
vi.mock('./ingest', () => ({
  ingest: mocks.ingest,
  isServerMsg: (x: unknown) => typeof x === 'object' && x !== null && typeof (x as { type?: unknown }).type === 'string',
}))
vi.mock('./fixture', () => ({ startFixture: () => Promise.resolve(() => {}) }))

// ------------------------------------------------------------------ fakes

class FakeSocket {
  static all: FakeSocket[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeSocket.all.push(this)
  }
  close(): void {
    this.closed = true
  }
  /** The server sends one message. */
  send(msg: ServerMsg): void {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
  /** The connection drops. */
  drop(): void {
    this.closed = true
    this.onclose?.()
  }
}

const snapshot = (tick: number): SnapshotMsg => ({ type: 'snapshot', tick, ts_ms: 1_000_000 + tick * 50 }) as unknown as SnapshotMsg
const event = (seq: number): EventMsg => ({ type: 'event', seq, tick: 1, day: 0, kind: 'move', agent_id: 'a', payload: {} }) as unknown as EventMsg
const metrics = (tick: number): MetricsMsg => ({ type: 'metrics', tick, day: 0, row: { tick } }) as unknown as MetricsMsg
const roster: ServerMsg = { type: 'roster', agents: [] }

function hello(runId: string, tick: number, lastSeq: number): HelloMsg {
  return {
    type: 'hello',
    run_id: runId,
    config: { render_delay_ticks: 2, population_cap: 4 },
    tick,
    day: 0,
    last_seq: lastSeq,
    paused: false,
    tick_seconds: 0.05,
    server_ts_ms: 0,
    status: 'running',
  } as unknown as HelloMsg
}

/** What the fake server holds when a request is answered; void/server/api.py semantics (exclusive since filters). */
const world = { maxTick: 0, maxSeq: 0, days: 0, ring: 600 }
/** While set, answers are computed at request time but delivered only when `release()` is called. */
let holding = false
let held: Array<() => void> = []
function release(): void {
  const h = held
  held = []
  for (const f of h) f()
}

function answer(path: string): unknown {
  const u = new URL(path, 'http://void.test')
  const q = (k: string, d: number) => (u.searchParams.has(k) ? Number(u.searchParams.get(k)) : d)
  if (u.pathname === '/api/events') {
    const since = q('since_seq', 0)
    const limit = q('limit', 500)
    expect(since).toBeGreaterThanOrEqual(0)
    const out: EventMsg[] = []
    for (let s = since + 1; s <= world.maxSeq && out.length < limit; s++) out.push(event(s))
    return { events: out, last_seq: world.maxSeq }
  }
  if (u.pathname === '/api/metrics') {
    if (u.searchParams.get('days')) {
      const days: DayMsg[] = []
      for (let d = 0; d < world.days; d++) days.push({ type: 'day', day: d, row: { day: d } } as unknown as DayMsg)
      return { days }
    }
    const since = q('since_tick', 0)
    const limit = q('limit', 5000)
    expect(since).toBeGreaterThanOrEqual(-1)
    const out: MetricsMsg[] = []
    for (let t = Math.max(1, since + 1); t <= world.maxTick && out.length < limit; t++) out.push(metrics(t))
    return { metrics: out }
  }
  if (u.pathname === '/api/snapshots') {
    const since = q('since_tick', 0)
    const limit = q('limit', 60)
    expect(since).toBeGreaterThanOrEqual(-1)
    const oldest = Math.max(1, world.maxTick - world.ring + 1)
    const out: SnapshotMsg[] = []
    for (let t = Math.max(oldest, since + 1); t <= world.maxTick && out.length < limit; t++) out.push(snapshot(t))
    return { snapshots: out }
  }
  throw new Error(`unexpected GET ${path}`)
}

function requested(): string[] {
  return mocks.apiGet.mock.calls.map((c) => c[0] as string)
}

function ingested(): ServerMsg[] {
  return mocks.ingest.mock.calls.map((c) => c[0] as ServerMsg)
}

function keysOf(type: 'snapshot' | 'event' | 'metrics', msgs = ingested()): number[] {
  const out: number[] = []
  for (const m of msgs) {
    if (m.type !== type) continue
    out.push(m.type === 'event' ? m.seq : m.tick)
  }
  return out
}

function range(from: number, to: number): number[] {
  const out: number[] = []
  for (let i = from; i <= to; i++) out.push(i)
  return out
}

/** Run-length encoding of the message types, e.g. ['hello', 'roster', 'snapshot', ...]. */
function runs(msgs: ServerMsg[]): string[] {
  const out: string[] = []
  for (const m of msgs) if (out[out.length - 1] !== m.type) out.push(m.type)
  return out
}

let client: WsClient | null = null

function connect(): FakeSocket {
  const n = FakeSocket.all.length
  if (!client) {
    client = new WsClient()
    client.start()
  } else {
    // the previous socket dropped: let the backoff timer fire
    vi.advanceTimersByTime(6000)
  }
  const ws = FakeSocket.all[n]
  if (!ws) throw new Error('no socket opened')
  ws.onopen?.()
  return ws
}

/** A fresh tab joins run r1 at tick 300 / seq 2555 while the server keeps ticking; returns the socket. */
async function freshLoad(): Promise<FakeSocket> {
  world.maxTick = 305 // the server is a little ahead by the time the GETs are answered
  world.maxSeq = 2650
  world.days = 2
  const ws = connect()
  ws.send(hello('r1', 300, 2555))
  ws.send(roster)
  for (let t = 241; t <= 300; t++) ws.send(snapshot(t)) // the preamble's 60-snapshot backlog
  for (let t = 301; t <= 310; t++) {
    ws.send(snapshot(t))
    ws.send(metrics(t))
    ws.send(event(2550 + (t - 300))) // 2551..2555 were already fetched
  }
  await client!.settled()
  return ws
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('location', { protocol: 'http:', host: 'void.test' })
  FakeSocket.all = []
  holding = false
  held = []
  world.maxTick = 0
  world.maxSeq = 0
  world.days = 0
  world.ring = 600
  mocks.ingest.mockReset()
  mocks.apiGet.mockReset()
  mocks.apiGet.mockImplementation((path: string) => {
    const body = answer(path)
    if (!holding) return Promise.resolve(body)
    return new Promise((resolve) => held.push(() => resolve(body)))
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

describe('fresh load', () => {
  it('backfills events, metrics, days and snapshots before live frames, in order and without duplicates', async () => {
    world.maxTick = 305
    world.maxSeq = 2650
    world.days = 2
    const ws = connect()
    ws.send(hello('r1', 300, 2555))
    ws.send(roster)
    // State messages apply at once; ordered ones wait for the backfill.
    expect(runs(ingested())).toEqual(['hello', 'roster'])
    for (let t = 241; t <= 300; t++) ws.send(snapshot(t))
    for (let t = 301; t <= 310; t++) {
      ws.send(snapshot(t))
      ws.send(metrics(t))
      ws.send(event(2550 + (t - 300)))
    }
    expect(runs(ingested())).toEqual(['hello', 'roster'])
    await client!.settled()

    expect(requested().sort()).toEqual(
      [
        '/api/events?since_seq=555&limit=1000',
        '/api/events?since_seq=1555&limit=1000',
        '/api/metrics?since_tick=-1&limit=1000',
        '/api/metrics?days=1',
        '/api/snapshots?since_tick=-1&limit=200',
        '/api/snapshots?since_tick=200&limit=200',
      ].sort(),
    )

    const msgs = ingested()
    // Each stream complete, strictly increasing, nothing twice: the newest 2000 events, every metrics row,
    // and the snapshot ring followed by the live frames (the replayed backlog is dropped).
    expect(keysOf('snapshot')).toEqual(range(1, 310))
    expect(keysOf('event')).toEqual(range(556, 2560))
    expect(keysOf('metrics')).toEqual(range(1, 310))
    expect(msgs.filter((m) => m.type === 'day').map((m) => (m as DayMsg).day)).toEqual([0, 1])
    // Backfill first (snapshots, events, metrics, days), then the queued live messages.
    expect(runs(msgs)).toEqual([
      'hello',
      'roster',
      'snapshot',
      'event',
      'metrics',
      'day',
      ...Array.from({ length: 5 }, () => ['snapshot', 'metrics', 'event']).flat(),
    ])
    expect(history.runId).toBe('r1')
    expect(history.renderDelayTicks).toBe(2)
    expect(client!.marks).toEqual({ seq: 2560, metricsTick: 310, snapTick: 310 })

    // Live traffic after the backfill: duplicates are still dropped.
    mocks.ingest.mockClear()
    ws.send(snapshot(310))
    ws.send(snapshot(311))
    ws.send(event(2560))
    ws.send(event(2561))
    expect(keysOf('snapshot')).toEqual([311])
    expect(keysOf('event')).toEqual([2561])
  })

  it('keeps only what the history ring holds when the server ring is larger', async () => {
    world.maxTick = 2000
    world.ring = 1000
    world.maxSeq = 10
    const ws = connect()
    ws.send(hello('r1', 2000, 10))
    await client!.settled()
    const snaps = requested().filter((p) => p.startsWith('/api/snapshots'))
    expect(snaps[0]).toBe(`/api/snapshots?since_tick=${2000 - history.ringCapacity}&limit=200`)
    expect(snaps.length).toBe(Math.ceil(history.ringCapacity / 200))
    expect(keysOf('snapshot')).toEqual(range(2000 - history.ringCapacity + 1, 2000))
    expect(requested()).toContain('/api/events?since_seq=0&limit=1000')
  })
})

describe('reconnect', () => {
  it('a same-run reconnect requests since_tick=lastMetricsTick and fetches no snapshots', async () => {
    const ws = await freshLoad()
    expect(client!.marks.metricsTick).toBe(310)
    mocks.apiGet.mockClear()
    mocks.ingest.mockClear()
    ws.drop()

    world.maxTick = 325
    world.maxSeq = 2700
    const ws2 = connect()
    ws2.send(hello('r1', 320, 2600))
    ws2.send(roster)
    for (let t = 261; t <= 320; t++) ws2.send(snapshot(t)) // the backlog again
    await client!.settled()

    expect(requested().sort()).toEqual(
      ['/api/events?since_seq=2560&limit=1000', '/api/metrics?since_tick=310&limit=1000', '/api/metrics?days=1'].sort(),
    )
    expect(keysOf('snapshot')).toEqual(range(311, 320))
    expect(keysOf('event')).toEqual(range(2561, 2700))
    expect(keysOf('metrics')).toEqual(range(311, 325))
  })

  it('a same-run reconnect with nothing missed fetches nothing', async () => {
    const ws = await freshLoad()
    mocks.apiGet.mockClear()
    mocks.ingest.mockClear()
    ws.drop()
    const ws2 = connect()
    ws2.send(hello('r1', 310, 2560))
    for (let t = 251; t <= 310; t++) ws2.send(snapshot(t))
    ws2.send(snapshot(311))
    await client!.settled()
    expect(requested()).toEqual([])
    expect(keysOf('snapshot')).toEqual([311])
  })

  it('a different run backfills from the start again', async () => {
    const ws = await freshLoad()
    mocks.apiGet.mockClear()
    mocks.ingest.mockClear()
    ws.drop()

    world.maxTick = 50
    world.maxSeq = 120
    world.days = 0
    const ws2 = connect()
    ws2.send(hello('r2', 50, 120))
    await client!.settled()
    expect(requested()).toContain('/api/snapshots?since_tick=-1&limit=200')
    expect(requested()).toContain('/api/metrics?since_tick=-1&limit=1000')
    expect(requested()).toContain('/api/events?since_seq=0&limit=1000')
    expect(keysOf('snapshot')).toEqual(range(1, 50))
    expect(keysOf('event')).toEqual(range(1, 120))
    expect(keysOf('metrics')).toEqual(range(1, 50))
    expect(history.runId).toBe('r2')
  })

  it('the same run id with ticks that went backwards is treated as a new run', async () => {
    const ws = await freshLoad()
    mocks.apiGet.mockClear()
    mocks.ingest.mockClear()
    ws.drop()

    world.maxTick = 20
    world.maxSeq = 40
    const ws2 = connect()
    ws2.send(hello('r1', 20, 40))
    await client!.settled()
    expect(requested()).toContain('/api/snapshots?since_tick=-1&limit=200')
    expect(keysOf('snapshot')).toEqual(range(1, 20))
    expect(keysOf('event')).toEqual(range(1, 40))
  })

  it('discards a backfill overtaken by a newer hello', async () => {
    world.maxTick = 1100
    world.maxSeq = 900
    holding = true
    const ws = connect()
    ws.send(hello('r1', 1100, 900))
    expect(held.length).toBeGreaterThan(0)
    ws.drop()

    holding = false
    world.maxTick = 50
    world.maxSeq = 60
    const ws2 = connect()
    ws2.send(hello('r2', 50, 60))
    await client!.settled()
    // The first connection's answers arrive late: none of them may be ingested.
    release()
    await new Promise<void>((r) => setImmediate(r)) // every microtask the late answers queue has run
    await client!.settled()
    expect(keysOf('snapshot')).toEqual(range(1, 50))
    expect(keysOf('event')).toEqual(range(1, 60))
    expect(keysOf('metrics')).toEqual(range(1, 50))
  })
})
