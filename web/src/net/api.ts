/**
 * HTTP helpers (DESIGN §15). The operator token comes from GET /api/session
 * (same-origin only), is kept in sessionStorage and sent as a Bearer token on
 * every POST. In fixture mode GETs are served from /fixtures/api.json and POSTs
 * are no-ops that log.
 */
import type { AgentDetail, CommandAck, SessionResponse, TreeResponse } from '../protocol'

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
  session: SessionResponse
  tree: TreeResponse
  agents: Record<string, AgentDetail>
  graveyard: string[]
}

let fixtureApi: Promise<FixtureApi> | null = null

function loadFixtureApi(): Promise<FixtureApi> {
  if (!fixtureApi) {
    fixtureApi = fetch('/fixtures/api.json').then((r) => {
      if (!r.ok) throw new ApiError(r.status, 'fixture api.json missing (run npm run gen:fixture)')
      return r.json() as Promise<FixtureApi>
    })
  }
  return fixtureApi
}

async function fixtureGet<T>(path: string): Promise<T> {
  const api = await loadFixtureApi()
  const clean = path.replace(/^\/api\//, '').split('?')[0]!
  if (clean === 'session') return api.session as T
  if (clean === 'tree') return api.tree as T
  if (clean === 'graveyard') return api.graveyard as T
  const m = /^agents\/([^/]+)$/.exec(clean)
  if (m) {
    const d = api.agents[decodeURIComponent(m[1]!)]
    if (!d) throw new ApiError(404, `no fixture detail for agent ${m[1]}`)
    return d as T
  }
  if (clean === 'events' || clean === 'metrics' || clean === 'snapshots') return { events: [], metrics: [], snapshots: [] } as T
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
