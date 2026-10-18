/**
 * HTTP helpers (DESIGN §15). The operator token comes from GET /api/session
 * (same-origin only), is kept in sessionStorage and sent as a Bearer token on
 * every POST. Each server process mints a new token, so a 401 clears the stored
 * one, fetches a fresh one and retries the request once. Errors carry the
 * server's `detail` (a string, or FastAPI's 422 list joined as "field: msg").
 * In fixture mode GETs are served from /fixtures/api.json and POSTs are no-ops
 * that log.
 */
import type {
  AgentDetail,
  CommandAck,
  GraveyardResponse,
  MemorySummaryResponse,
  NoteRecord,
  PolicyResponse,
  PracticesResponse,
  RunStatus,
  SelfVersion,
  SessionResponse,
  StatusMsg,
  TreeResponse,
} from '../protocol'
import { history, SLOT_STRIDE } from '../state/history'
import { useStore } from '../state/store'
import { treeFromRoster } from '../state/lineage'

export const FIXTURE_MODE = import.meta.env.VITE_FEED === 'fixture'
const TOKEN_KEY = 'void.operatorToken'

let tokenPromise: Promise<string | null> | null = null
/** Fallback when sessionStorage is unavailable (private mode). */
let memToken: string | null = null

function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memToken
  } catch {
    return memToken
  }
}

/** Forget the operator token (after a 401: the server restarted and minted a new one). */
export function clearToken(): void {
  memToken = null
  try {
    sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

export function ensureToken(): Promise<string | null> {
  if (FIXTURE_MODE) return Promise.resolve('fixture-token')
  const stored = readStoredToken()
  if (stored) return Promise.resolve(stored)
  if (!tokenPromise) {
    tokenPromise = fetch('/api/session', { credentials: 'same-origin' })
      .then(async (r) => {
        if (!r.ok) return null
        const j = (await r.json()) as SessionResponse
        memToken = j.token
        try {
          sessionStorage.setItem(TOKEN_KEY, j.token)
        } catch {
          /* private mode: keep it in memory only */
        }
        return j.token
      })
      .catch(() => null)
      .finally(() => {
        tokenPromise = null
      })
  }
  return tokenPromise
}

export class ApiError extends Error {
  status: number
  /** The server's explanation (FastAPI `detail`), or null when the body had none. */
  detail: string | null
  constructor(status: number, message: string, detail: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** FastAPI error bodies: `{detail: "..."}`, or on 422 `{detail: [{loc, msg}, ...]}` joined as "field: msg; ...". */
export function errorDetail(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null
  const d = (body as { detail?: unknown }).detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) {
    const parts = d.map((item: unknown) => {
      if (!item || typeof item !== 'object') return String(item)
      const o = item as { loc?: unknown; msg?: unknown }
      // loc is e.g. ["body", "arrival", "name"]; the leading "body"/"query"/"path" says nothing to an operator
      const loc = Array.isArray(o.loc) ? o.loc.filter((x, i) => !(i === 0 && (x === 'body' || x === 'query' || x === 'path'))).join('.') : ''
      const msg = typeof o.msg === 'string' ? o.msg : JSON.stringify(item)
      return loc ? `${loc}: ${msg}` : msg
    })
    return parts.length ? parts.join('; ') : null
  }
  return d === undefined || d === null ? null : JSON.stringify(d)
}

async function errorFrom(r: Response, path: string): Promise<ApiError> {
  let detail: string | null = null
  try {
    const text = await r.text()
    if (text) {
      try {
        detail = errorDetail(JSON.parse(text))
      } catch {
        detail = text.trim().slice(0, 300) || null
      }
    }
  } catch {
    /* body unreadable: the status alone */
  }
  return new ApiError(r.status, detail ? `${detail} (${r.status})` : `${path}: ${r.status}`, detail)
}

// ------------------------------------------------------------ fixture backing

/**
 * web/public/fixtures/api.json: the /api answers at the end of the recorded run. `void mock-feed` writes it next to
 * mock.jsonl (void/cli.py record_feed: agents, tree, graveyard, gadgets, tasks and the memory reads `policy`,
 * `memory_summary` and `practices`); scripts/gen-fixture.mjs writes a synthetic one with a `session` and without the
 * memory reads. Every key is optional here, and so is the file: what it lacks is derived from the stream or answered 404.
 */
export interface FixtureApi {
  run_id?: string
  session?: SessionResponse
  tree?: TreeResponse
  agents?: Record<string, AgentDetail>
  graveyard?: GraveyardResponse
  /** GET /api/agents/{id}/policy per agent id */
  policy?: Record<string, PolicyResponse>
  /** GET /api/memory/summary */
  memory_summary?: MemorySummaryResponse
  /** GET /api/practices (the route's default limit, newest first) */
  practices?: PracticesResponse
}

let fixtureApi: Promise<FixtureApi | null> | null = null

/** Fetched once; a missing or unreadable api.json leaves fixture mode on the stream-derived answers. */
function loadFixtureApi(): Promise<FixtureApi | null> {
  if (!fixtureApi) {
    fixtureApi = fetch('/fixtures/api.json')
      .then((r) => (r.ok ? (r.json() as Promise<FixtureApi>) : null))
      .catch(() => null)
  }
  return fixtureApi
}

/** Agent detail assembled from the live stream when api.json does not cover this run. */
function deriveAgentDetail(id: string): AgentDetail {
  const s = useStore.getState()
  const rec = s.rosterById.get(id)
  if (!rec) throw new ApiError(404, `unknown agent ${id}`)
  const stat = s.agentStats.get(id)
  const notes: NoteRecord[] = []
  const self_versions: SelfVersion[] = []
  s.events.forEach((e) => {
    if (e.agent_id !== id) return
    const p = e.payload as Record<string, unknown>
    if (e.kind === 'remember' && typeof p.title === 'string') {
      notes.push({
        note_id: typeof p.note_id === 'string' ? p.note_id : `${id}-${e.seq}`,
        title: p.title,
        body: '(note body is only served by /api/agents/{id}; the feed carries the title)',
        created_tick: e.tick,
        channel: typeof p.channel === 'string' ? p.channel : 'observed',
        hop: typeof p.hop === 'number' ? p.hop : 0,
        importance: 0,
        archived: false,
      })
    } else if (e.kind === 'revise_self' && typeof p.version === 'number') {
      self_versions.push({ version: p.version, tick: e.tick, summary: `(version ${p.version} recorded at tick ${e.tick}; text is only served by /api/agents/{id})` })
    }
  })
  const balance_series: Array<{ tick: number; balance_usd: number }> = []
  const slot = history.agentIndex.get(id)
  if (slot !== undefined) {
    const step = Math.max(1, Math.ceil(history.count / 240))
    for (let i = 0; i < history.count; i += step) {
      const f = history.frameAt(i)
      if (slot < f.capacity && f.ids[slot] === id) balance_series.push({ tick: f.tick, balance_usd: f.data[slot * SLOT_STRIDE + 5]! })
    }
  }
  return {
    agent: { ...rec, balance_usd: stat?.balance ?? 0, stress: stat?.stress ?? 0, x: stat?.x ?? 0, y: stat?.y ?? 0 },
    self_summary: null,
    self_versions,
    notes,
    calls: [],
    balance_series,
    children: s.roster.filter((a) => a.parent_id === id).map((a) => a.id),
  }
}

async function fixtureGet<T>(path: string): Promise<T> {
  const loaded = await loadFixtureApi()
  const runId = useStore.getState().runId
  // api.json belongs to the recording next to it; ignore it when a different run is playing.
  const api = loaded && (!loaded.run_id || !runId || loaded.run_id === runId) ? loaded : null
  const [clean = '', query = ''] = path.replace(/^\/api\//, '').split('?')
  if (clean === 'session') return (api?.session ?? { token: 'fixture-token' }) as T
  if (clean === 'tree') return (api?.tree ?? { roots: treeFromRoster(useStore.getState().roster) }) as T
  if (clean === 'graveyard') return (api?.graveyard ?? { agents: [] }) as T
  const m = /^agents\/([^/]+)$/.exec(clean)
  if (m) {
    const id = decodeURIComponent(m[1]!)
    return (api?.agents?.[id] ?? deriveAgentDetail(id)) as T
  }
  const pol = /^agents\/([^/]+)\/policy$/.exec(clean)
  if (pol) {
    const id = decodeURIComponent(pol[1]!)
    const p = api?.policy?.[id]
    if (p) return p as T
    throw new ApiError(404, `fixture has no policy for ${id}`)
  }
  if (clean === 'memory/summary' && api?.memory_summary) return api.memory_summary as T
  if (clean === 'practices' && api?.practices) {
    // recorded with the route's default limit; a smaller ?limit= keeps the newest rows, like the route
    const limit = Number(new URLSearchParams(query).get('limit'))
    const events = api.practices.events
    return (Number.isInteger(limit) && limit >= 1 && limit < events.length ? { events: events.slice(0, limit) } : api.practices) as T
  }
  if (clean === 'events') return { events: [], last_seq: 0 } as T
  if (clean === 'metrics') return { metrics: [], days: [] } as T
  if (clean === 'snapshots') return { snapshots: [] } as T
  throw new ApiError(404, `fixture has no ${path}`)
}

// ------------------------------------------------------------ public API

export async function apiGet<T>(path: string): Promise<T> {
  if (FIXTURE_MODE) return fixtureGet<T>(path)
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) throw await errorFrom(r, path)
  return (await r.json()) as T
}

function send(path: string, body: unknown, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  return fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export async function apiPost<T = CommandAck>(path: string, body?: unknown): Promise<T> {
  if (FIXTURE_MODE) {
    console.info('[fixture] POST (no-op)', path, body ?? '')
    return { cmd_id: 'fixture', will_apply_at_tick: -1 } as T
  }
  let r = await send(path, body, await ensureToken())
  if (r.status === 401) {
    // a restarted server minted a new token (void/sim/loop.py): fetch it once and retry once
    clearToken()
    const fresh = await ensureToken()
    if (fresh) r = await send(path, body, fresh)
    if (r.status === 401) clearToken()
  }
  if (!r.ok) throw await errorFrom(r, path)
  const text = await r.text()
  return (text ? JSON.parse(text) : {}) as T
}

// ------------------------------------------------------------ operator feedback

/**
 * Bounds of the operator POST bodies, mirrored from the pydantic models in void/server/api.py
 * (api.test.ts reads that file and fails when they drift).
 */
export const LIMITS = {
  taskTitle: 80,
  taskDescription: 600,
  taskReward: { min: 0, max: 1000 },
  arrivalName: 40,
  arrivalBalance: { min: 0, max: 1000 },
  epochDay: { min: 0 },
  scarcity: { min: 0, max: 10 },
  weather: { min: -1, max: 1 },
  durationDays: { min: 1, max: 365 },
  /** exclusive minimum: a grant must be more than 0 */
  benefactorAmount: { min: 0, max: 100 },
  tickSeconds: { min: 0, max: 3600 },
} as const

/** POST /api/control/step answers with the tick it ran (void/server/api.py control_step). */
export interface StepReport {
  tick: number
  day: number
  status: string
  calls: number
  deaths: string[]
  births: string[]
}

/** Why operator controls are off, or null when the run takes commands (every command POST answers 409 otherwise). */
export function controlsBlockedReason(runStatus: RunStatus | null): string | null {
  if (runStatus === null) return 'waiting for the server'
  return runStatus === 'running' ? null : `run is ${runStatus}`
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function people(ids: string[], nameOf: (id: string) => string): string {
  return ids.length ? ` (${ids.map(nameOf).join(', ')})` : ''
}

/**
 * One line saying what a POST did. Commands only enqueue (DESIGN §14.1) and say when they apply;
 * pause, resume and speed act at once and answer the loop's status; step answers what its tick did.
 */
export function describeResult(result: unknown, nameOf: (id: string) => string = (id) => id): string {
  if (FIXTURE_MODE) return 'fixture mode: logged, not sent'
  if (!result || typeof result !== 'object') return 'done'
  const x = result as Record<string, unknown>
  if (typeof x.tick === 'number' && typeof x.calls === 'number' && Array.isArray(x.deaths) && Array.isArray(x.births)) {
    const s = x as unknown as StepReport
    const ended = s.status && s.status !== 'running' ? `; run is ${s.status}` : ''
    return `ran tick ${s.tick} (day ${s.day}): ${plural(s.calls, 'call')}, ${plural(s.deaths.length, 'death')}${people(s.deaths, nameOf)}, ${plural(s.births.length, 'birth')}${people(s.births, nameOf)}${ended}`
  }
  if (typeof x.paused === 'boolean' && typeof x.tick_seconds === 'number') {
    const s = x as unknown as StatusMsg
    const ended = s.status && s.status !== 'running' ? ` · run is ${s.status}` : ''
    return `${s.paused ? 'paused' : 'running'} · ${s.tick_seconds} s per tick${ended}`
  }
  if (typeof x.will_apply_at_tick === 'number') return `queued as command ${String(x.cmd_id ?? '?')}, applies at tick ${x.will_apply_at_tick}`
  return 'done'
}
