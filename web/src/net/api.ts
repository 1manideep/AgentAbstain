/**
 * HTTP helpers (DESIGN §15). The operator token comes from GET /api/session
 * (same-origin only), is kept in sessionStorage and sent as a Bearer token on
 * every POST. In fixture mode GETs are served from /fixtures/api.json and POSTs
 * are no-ops that log.
 */
import type { AgentDetail, CommandAck, GraveyardResponse, NoteRecord, SelfVersion, SessionResponse, TreeResponse } from '../protocol'
import { history, SLOT_STRIDE } from '../state/history'
import { useStore } from '../state/store'
import { treeFromRoster } from '../state/lineage'

export const FIXTURE_MODE = import.meta.env.VITE_FEED === 'fixture'
const TOKEN_KEY = 'void.operatorToken'

let tokenPromise: Promise<string | null> | null = null

function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
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
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// ------------------------------------------------------------ fixture backing

export interface FixtureApi {
  run_id?: string
  session: SessionResponse
  tree: TreeResponse
  agents: Record<string, AgentDetail>
  graveyard: GraveyardResponse
}

let fixtureApi: Promise<FixtureApi | null> | null = null

/** api.json is written by scripts/gen-fixture.mjs; a genuine `void mock-feed` recording has none, so it is optional. */
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
  // api.json belongs to the synthetic fixture; ignore it when a different run is playing.
  const api = loaded && (!loaded.run_id || !runId || loaded.run_id === runId) ? loaded : null
  const clean = path.replace(/^\/api\//, '').split('?')[0]!
  if (clean === 'session') return (api?.session ?? { token: 'fixture-token' }) as T
  if (clean === 'tree') return (api?.tree ?? { roots: treeFromRoster(useStore.getState().roster) }) as T
  if (clean === 'graveyard') return (api?.graveyard ?? { agents: [] }) as T
  const m = /^agents\/([^/]+)$/.exec(clean)
  if (m) {
    const id = decodeURIComponent(m[1]!)
    return (api?.agents[id] ?? deriveAgentDetail(id)) as T
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
  if (!r.ok) throw new ApiError(r.status, `${path}: ${r.status}`)
  return (await r.json()) as T
}

export async function apiPost<T = CommandAck>(path: string, body?: unknown): Promise<T> {
  if (FIXTURE_MODE) {
    console.info('[fixture] POST (no-op)', path, body ?? '')
    return { cmd_id: 'fixture', will_apply_at_tick: -1 } as T
  }
  const token = await ensureToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 401) {
    try {
      sessionStorage.removeItem(TOKEN_KEY)
    } catch {
      /* ignore */
    }
  }
  if (!r.ok) throw new ApiError(r.status, `${path}: ${r.status}`)
  const text = await r.text()
  return (text ? JSON.parse(text) : {}) as T
}
