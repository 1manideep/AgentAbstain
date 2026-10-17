/**
 * WebSocket client (DESIGN §15): reconnect with 250 ms → 5 s jittered backoff, `hello` handling with
 * catch-up over the HTTP API, idempotent replay, and `conn` state in the store.
 *
 * On `hello` for a run this client has not backfilled (a fresh tab, a different run, or the same run id
 * whose ticks went backwards because the run was started again) the control room is filled before any
 * live frame: the newest ≤ 2000 events (GET /api/events?since_seq=, pages of 1000), every per-tick
 * metrics row (GET /api/metrics?since_tick=-1, paged), the day rows (GET /api/metrics?days=1) and the
 * server's snapshot ring (GET /api/snapshots?since_tick=, paged), which seeds the History buffer and the
 * scrub range. A same-run reconnect fetches only what it missed: events after the last seq and metrics
 * after the last metrics tick (both server filters are exclusive), plus the day rows.
 *
 * While a fetch runs, ordered live messages (snapshot, event, metrics, day) wait in `pending`; state
 * messages (roster, gadgets, tasks, chronicle, status) apply at once, so the backfilled events and
 * snapshots see the roster. Every ordered message passes one high-water mark per stream (`marks`), so
 * the replayed 60-snapshot backlog and any overlap between a fetch and the live stream are dropped
 * instead of being appended or counted twice. A catch-up overtaken by a newer `hello` is discarded.
 */
import type {
  DayMsg,
  EventMsg,
  EventsResponse,
  HelloMsg,
  MetricsDaysResponse,
  MetricsMsg,
  MetricsResponse,
  ServerMsg,
  SnapshotMsg,
} from '../protocol'
import { fxQueue } from '../scene/fx'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { apiGet, ensureToken, FIXTURE_MODE } from './api'
import { startFixture } from './fixture'
import { ingest, isServerMsg } from './ingest'

const BACKOFF_MIN = 250
const BACKOFF_MAX = 5000

/** Events backfilled on a fresh load: the store's event ring holds 2000. */
export const EVENT_BACKFILL = 2000
export const EVENT_PAGE = 1000
/** A same-run reconnect pages events from the last seq, at most this many pages. */
export const EVENT_CATCHUP_PAGES = 20
export const METRICS_PAGE = 1000
/** Safety bound on metrics pages (each page must also advance, or paging stops). */
export const METRICS_MAX_PAGES = 1000
export const SNAPSHOT_PAGE = 200

/** GET /api/snapshots?since_tick=&limit= (void/server/api.py): snapshots with tick > since_tick, oldest first. */
interface SnapshotsResponse {
  snapshots: SnapshotMsg[]
}

/** High-water marks of what has been ingested, one per ordered stream. */
export interface Marks {
  seq: number
  metricsTick: number
  snapTick: number
}

/** Message types whose order matters: they wait in `pending` while a catch-up runs. */
const ORDERED: ReadonlySet<string> = new Set(['snapshot', 'event', 'metrics', 'day'])

type Live = () => boolean

export function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

async function attempt(label: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn()
  } catch (err) {
    console.warn(`${label} catch-up failed`, err)
  }
}

/** Events with seq > since in seq order, until a short page, `until` is reached, or `maxPages`. */
async function pageEvents(since: number, until: number, maxPages: number, live: Live, out: EventMsg[]): Promise<void> {
  for (let page = 0; page < maxPages && live(); page++) {
    const r = await apiGet<EventsResponse>(`/api/events?since_seq=${since}&limit=${EVENT_PAGE}`)
    const list = r.events ?? []
    for (const e of list) out.push({ ...e, type: 'event' } as EventMsg)
    if (list.length < EVENT_PAGE) return
    const last = list[list.length - 1]!.seq
    if (last <= since || last >= until) return
    since = last
  }
}

/** Per-tick metrics rows with tick > since, until a short page, `until` is reached, or the page bound. */
async function pageMetrics(since: number, until: number, live: Live, out: MetricsMsg[]): Promise<void> {
  for (let page = 0; page < METRICS_MAX_PAGES && live(); page++) {
    const r = await apiGet<MetricsResponse>(`/api/metrics?since_tick=${since}&limit=${METRICS_PAGE}`)
    const list = r.metrics ?? []
    for (const row of list) out.push({ ...row, type: 'metrics' } as MetricsMsg)
    if (list.length < METRICS_PAGE) return
    const last = list[list.length - 1]!.tick
    if (last <= since || last >= until) return
    since = last
  }
}

/** Persisted snapshots with tick > since, until a short page, `until` is reached, or `maxPages`. */
async function pageSnapshots(since: number, until: number, maxPages: number, live: Live, out: SnapshotMsg[]): Promise<void> {
  for (let page = 0; page < maxPages && live(); page++) {
    const r = await apiGet<SnapshotsResponse>(`/api/snapshots?since_tick=${since}&limit=${SNAPSHOT_PAGE}`)
    const list = r.snapshots ?? []
    for (const s of list) out.push({ ...s, type: 'snapshot' } as SnapshotMsg)
    if (list.length < SNAPSHOT_PAGE) return
    const last = list[list.length - 1]!.tick
    if (last <= since || last >= until) return
    since = last
  }
}

async function fetchDays(out: DayMsg[]): Promise<void> {
  const r = await apiGet<MetricsDaysResponse>('/api/metrics?days=1')
  for (const d of r.days ?? []) out.push({ type: 'day', day: d.day, row: d.row })
}

export class WsClient {
  private ws: WebSocket | null = null
  private attempt = 0
  private stopped = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private catchingUp: Promise<void> | null = null
  /** Bumped by every `hello` and by `stop`; a catch-up started under an older value is discarded. */
  private gen = 0
  /** The run this client has backfilled; null until a backfill finished. */
  private seededRun: string | null = null
  /** Buffer of ordered live messages while a catch-up fetch is running. */
  private pending: ServerMsg[] = []
  readonly marks: Marks = { seq: 0, metricsTick: -1, snapTick: -1 }

  start(): void {
    this.stopped = false
    void ensureToken()
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.gen++
    this.pending = []
    this.catchingUp = null
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.ws?.close()
    this.ws = null
    useStore.getState().setConn('closed')
  }

  /** Resolves once the running catch-up (if any) has been ingested along with the queued live messages. */
  settled(): Promise<void> {
    return this.catchingUp ?? Promise.resolve()
  }

  private connect(): void {
    if (this.stopped) return
    useStore.getState().setConn(this.attempt === 0 ? 'connecting' : 'reconnecting', 'ws')
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl())
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    ws.onopen = () => {
      this.attempt = 0
      useStore.getState().setConn('open')
    }
    ws.onmessage = (ev) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(typeof ev.data === 'string' ? ev.data : '')
      } catch {
        return
      }
      if (!isServerMsg(parsed)) return
      if (parsed.type === 'hello') this.onHello(parsed)
      else this.deliver(parsed)
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      this.scheduleReconnect()
    }
    ws.onerror = () => {
      // onclose follows; nothing to do here
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.timer) return
    const base = Math.min(BACKOFF_MAX, BACKOFF_MIN * 2 ** this.attempt)
    const jitter = base * (0.75 + Math.random() * 0.5)
    this.attempt++
    useStore.getState().setConn('reconnecting')
    this.timer = setTimeout(() => {
      this.timer = null
      this.connect()
    }, Math.min(BACKOFF_MAX, jitter))
  }

  private deliver(msg: ServerMsg): void {
    if (this.catchingUp && ORDERED.has(msg.type)) {
      this.pending.push(msg)
      return
    }
    this.feed(msg)
  }

  /** Ingest one message unless its ordered stream has already passed it (by seq or tick). */
  private feed(msg: ServerMsg): void {
    const k = this.marks
    if (msg.type === 'event') {
      if (msg.seq <= k.seq) return
      k.seq = msg.seq
    } else if (msg.type === 'metrics') {
      if (msg.tick <= k.metricsTick) return
      k.metricsTick = msg.tick
    } else if (msg.type === 'snapshot') {
      if (msg.tick <= k.snapTick) return
      k.snapTick = msg.tick
    }
    ingest(msg)
  }

  private onHello(m: HelloMsg): void {
    const gen = ++this.gen
    // Whatever an older connection queued is superseded by this hello's own catch-up.
    this.pending = []
    this.catchingUp = null
    const k = this.marks
    const s = useStore.getState()
    const knownRun = this.seededRun ?? s.runId
    const newestTick = Math.max(k.snapTick, k.metricsTick, history.newest()?.tick ?? -1)
    if (knownRun === m.run_id && m.tick < newestTick) {
      // The same run id with ticks that went backwards: the run was started again. Begin from nothing
      // (the marks are taken again from the emptied store below).
      history.reset()
      s.resetRun()
      fxQueue.clear()
      this.seededRun = null
    }
    const sameRun = this.seededRun === m.run_id
    history.beginRun(m.run_id)
    ingest(m)
    history.setRenderDelayTicks(m.config.render_delay_ticks)

    if (sameRun) {
      const behindEvents = m.last_seq > k.seq
      const behindMetrics = m.tick > k.metricsTick
      if (!behindEvents && !behindMetrics) {
        history.snap()
        return
      }
      this.runCatchUp(gen, this.catchUp(m, gen, behindEvents, behindMetrics), null)
      return
    }

    // A run this client has not backfilled: start the marks from what the store already holds (nothing,
    // unless it kept this run from an earlier client) and fill the control room before live frames.
    this.seededRun = null
    const st = useStore.getState()
    k.seq = st.lastSeq
    k.metricsTick = st.series.lastMetricsTick
    k.snapTick = history.newest()?.tick ?? -1
    this.runCatchUp(gen, this.backfill(m, gen), m.run_id)
  }

  /** Hold ordered live messages until `work` has ingested its rows, then flush them and snap to live. */
  private runCatchUp(gen: number, work: Promise<void>, seeds: string | null): void {
    this.catchingUp = work
      .catch((err: unknown) => console.warn('catch-up failed', err))
      .finally(() => {
        if (gen !== this.gen) return
        this.catchingUp = null
        const queued = this.pending
        this.pending = []
        for (const q of queued) this.feed(q)
        if (seeds !== null) this.seededRun = seeds
        history.snap()
      })
  }

  /** Fresh load or a different run: snapshot ring, newest events, all metrics rows and day rows. */
  private async backfill(m: HelloMsg, gen: number): Promise<void> {
    const live: Live = () => gen === this.gen
    const snaps: SnapshotMsg[] = []
    const events: EventMsg[] = []
    const metrics: MetricsMsg[] = []
    const days: DayMsg[] = []
    const ring = history.ringCapacity
    await Promise.all([
      attempt('snapshot', () => pageSnapshots(Math.max(-1, m.tick - ring), m.tick, Math.ceil(ring / SNAPSHOT_PAGE) + 1, live, snaps)),
      attempt('event', () =>
        pageEvents(Math.max(0, m.last_seq - EVENT_BACKFILL), m.last_seq, Math.ceil(EVENT_BACKFILL / EVENT_PAGE) + 1, live, events),
      ),
      attempt('metrics', () => pageMetrics(-1, m.tick, live, metrics)),
      attempt('day', () => fetchDays(days)),
    ])
    if (!live()) return
    for (const x of snaps.length > ring ? snaps.slice(snaps.length - ring) : snaps) this.feed(x)
    for (const x of events) this.feed(x)
    for (const x of metrics) this.feed(x)
    for (const x of days) this.feed(x)
  }

  /** Same-run reconnect: events after the last seq, metrics after the last metrics tick, and day rows. */
  private async catchUp(m: HelloMsg, gen: number, behindEvents: boolean, behindMetrics: boolean): Promise<void> {
    const live: Live = () => gen === this.gen
    const k = this.marks
    const events: EventMsg[] = []
    const metrics: MetricsMsg[] = []
    const days: DayMsg[] = []
    const jobs: Promise<void>[] = []
    if (behindEvents) jobs.push(attempt('event', () => pageEvents(k.seq, m.last_seq, EVENT_CATCHUP_PAGES, live, events)))
    if (behindMetrics) {
      jobs.push(attempt('metrics', () => pageMetrics(k.metricsTick, m.tick, live, metrics)))
      jobs.push(attempt('day', () => fetchDays(days)))
    }
    await Promise.all(jobs)
    if (!live()) return
    for (const x of events) this.feed(x)
    for (const x of metrics) this.feed(x)
    for (const x of days) this.feed(x)
  }
}

let client: WsClient | null = null
let stopFixture: (() => void) | null = null

/** Start the configured feed (fixture replay or live WebSocket). Returns a stop function. */
export function startFeed(): () => void {
  if (FIXTURE_MODE) {
    let cancelled = false
    void startFixture(() => cancelled).then((stop) => {
      if (cancelled) stop()
      else stopFixture = stop
    })
    return () => {
      cancelled = true
      stopFixture?.()
      stopFixture = null
    }
  }
  client = new WsClient()
  client.start()
  return () => {
    client?.stop()
    client = null
  }
}
