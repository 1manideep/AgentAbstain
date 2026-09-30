/**
 * WebSocket client (DESIGN §15): reconnect with 250 ms → 5 s jittered backoff,
 * `hello` handling with catch-up through GET /api/events?since_seq= and
 * GET /api/metrics?since_tick= when the client is behind, idempotent event
 * replay by seq (the ring drops seq <= lastSeq), and `conn` state in the store.
 */
import type { EventMsg, EventsResponse, HelloMsg, MetricsMsg, MetricsResponse, ServerMsg } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { apiGet, ensureToken, FIXTURE_MODE } from './api'
import { startFixture } from './fixture'
import { ingest, isServerMsg } from './ingest'

const BACKOFF_MIN = 250
const BACKOFF_MAX = 5000

export function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

export class WsClient {
  private ws: WebSocket | null = null
  private attempt = 0
  private stopped = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private catchingUp: Promise<void> | null = null

  start(): void {
    this.stopped = false
    void ensureToken()
    this.connect()
  }

  stop(): void {
    this.stopped = true
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.ws?.close()
    this.ws = null
    useStore.getState().setConn('closed')
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

  /** Buffer live messages while a catch-up fetch is running so seq order holds. */
  private pending: ServerMsg[] = []

  private deliver(msg: ServerMsg): void {
    if (this.catchingUp) {
      this.pending.push(msg)
      return
    }
    ingest(msg)
  }

  private onHello(m: HelloMsg): void {
    const s = useStore.getState()
    const sameRun = s.runId === m.run_id
    const lastSeq = sameRun ? s.lastSeq : 0
    const lastMetricsTick = sameRun ? s.series.lastMetricsTick : -1
    ingest(m)
    const behindEvents = sameRun && m.last_seq > lastSeq
    const behindMetrics = sameRun && m.tick > lastMetricsTick + 1
    if (!behindEvents && !behindMetrics) {
      history.snap()
      return
    }
    this.catchingUp = this.catchUp(lastSeq, lastMetricsTick, behindEvents, behindMetrics).finally(() => {
      this.catchingUp = null
      const queued = this.pending
      this.pending = []
      for (const q of queued) ingest(q)
      history.snap()
    })
  }

  private async catchUp(sinceSeq: number, sinceTick: number, events: boolean, metrics: boolean): Promise<void> {
    if (events) {
      try {
        let since = sinceSeq
        for (let page = 0; page < 20; page++) {
          const r = await apiGet<EventsResponse>(`/api/events?since_seq=${since}&limit=1000`)
          const list = r.events ?? []
          for (const e of list) ingest({ ...e, type: 'event' } as EventMsg)
          if (list.length < 1000) break
          since = list[list.length - 1]!.seq
        }
      } catch (err) {
        console.warn('event catch-up failed', err)
      }
    }
    if (metrics) {
      try {
        const r = await apiGet<MetricsResponse>(`/api/metrics?since_tick=${sinceTick + 1}`)
        for (const row of r.metrics ?? []) ingest({ ...row, type: 'metrics' } as MetricsMsg)
      } catch (err) {
        console.warn('metrics catch-up failed', err)
      }
    }
  }
}

let client: WsClient | null = null
let stopFixture: (() => void) | null = null

/** Start the configured feed (fixture replay or live WebSocket). Returns a stop function. */
export function startFeed(): () => void {
  if (FIXTURE_MODE) {
    let cancelled = false
    void startFixture().then((stop) => {
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
