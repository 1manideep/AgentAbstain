import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiPost, clearToken, controlsBlockedReason, describeResult, ensureToken, errorDetail, LIMITS } from './api'

/** sessionStorage stand-in (the test environment is node). */
class MemoryStorage {
  private m = new Map<string, string>()
  getItem(k: string): string | null {
    return this.m.get(k) ?? null
  }
  setItem(k: string, v: string): void {
    this.m.set(k, v)
  }
  removeItem(k: string): void {
    this.m.delete(k)
  }
}

interface Call {
  url: string
  method: string
  auth: string | null
}

let storage: MemoryStorage
let calls: Call[]

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** Install a fetch that records each call and answers from `route`. */
function mockFetch(route: (c: Call, n: number) => Response) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>
    const c: Call = { url: String(input), method: init?.method ?? 'GET', auth: headers.Authorization ?? null }
    calls.push(c)
    return route(c, calls.length)
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

beforeEach(() => {
  storage = new MemoryStorage()
  calls = []
  vi.stubGlobal('sessionStorage', storage)
  clearToken()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('server error details', () => {
  it('puts a FastAPI 422 detail into ApiError.message as "field: msg"', async () => {
    storage.setItem('void.operatorToken', 'tok')
    mockFetch(() =>
      json(422, {
        detail: [
          { type: 'string_too_long', loc: ['body', 'title'], msg: 'String should have at most 80 characters', input: 'x'.repeat(81), ctx: { max_length: 80 } },
          { type: 'less_than_equal', loc: ['body', 'reward_usd'], msg: 'Input should be less than or equal to 1000', input: 5000 },
        ],
      }),
    )
    const err = await apiPost('/api/tasks', { title: 'x'.repeat(81), description: '', reward_usd: 5000 }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const e = err as ApiError
    expect(e.status).toBe(422)
    expect(e.detail).toBe('title: String should have at most 80 characters; reward_usd: Input should be less than or equal to 1000')
    expect(e.message).toContain('title: String should have at most 80 characters')
    expect(e.message).toContain('(422)')
    expect(calls).toHaveLength(1)
    expect(calls[0]!.auth).toBe('Bearer tok')
  })

  it('carries a plain string detail (409 when the run has ended)', async () => {
    storage.setItem('void.operatorToken', 'tok')
    mockFetch(() => json(409, { detail: 'run is completed' }))
    const err = (await apiPost('/api/control/epoch', { kind: 'storm', duration_days: 1 }).catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(409)
    expect(err.detail).toBe('run is completed')
    expect(err.message).toBe('run is completed (409)')
  })

  it('falls back to the body text, then to the path and status', async () => {
    storage.setItem('void.operatorToken', 'tok')
    mockFetch((_c, n) => (n === 1 ? new Response('Internal Server Error', { status: 500 }) : new Response('', { status: 503 })))
    const a = (await apiPost('/api/control/step').catch((e: unknown) => e)) as ApiError
    expect(a.message).toBe('Internal Server Error (500)')
    const b = (await apiGet('/api/state').catch((e: unknown) => e)) as ApiError
    expect(b.status).toBe(503)
    expect(b.detail).toBeNull()
    expect(b.message).toBe('/api/state: 503')
  })

  it('reads every detail shape FastAPI produces', () => {
    expect(errorDetail({ detail: 'unknown agent' })).toBe('unknown agent')
    // a model_validator error is located at the body itself
    expect(errorDetail({ detail: [{ loc: ['body'], msg: 'Value error, arrival epochs need an `arrival` block' }] })).toBe('Value error, arrival epochs need an `arrival` block')
    expect(errorDetail({ detail: [{ loc: ['body', 'arrival', 'name'], msg: 'String should have at most 40 characters' }] })).toBe('arrival.name: String should have at most 40 characters')
    expect(errorDetail({ detail: [{ loc: ['query', 'limit'], msg: 'too big' }] })).toBe('limit: too big')
    expect(errorDetail({ nothing: true })).toBeNull()
    expect(errorDetail(null)).toBeNull()
  })
})

describe('401 retry', () => {
  it('clears the stale token, fetches a fresh one and retries once', async () => {
    storage.setItem('void.operatorToken', 'stale')
    mockFetch((c) => {
      if (c.url === '/api/session') return json(200, { token: 'fresh' })
      return c.auth === 'Bearer fresh' ? json(202, { cmd_id: 7, will_apply_at_tick: 12 }) : json(401, { detail: 'operator token required' })
    })
    const ack = await apiPost('/api/tasks', { title: 't', description: '', reward_usd: 0.25 })
    expect(ack).toEqual({ cmd_id: 7, will_apply_at_tick: 12 })
    expect(calls.map((c) => `${c.method} ${c.url} ${c.auth ?? '-'}`)).toEqual(['POST /api/tasks Bearer stale', 'GET /api/session -', 'POST /api/tasks Bearer fresh'])
    expect(storage.getItem('void.operatorToken')).toBe('fresh')
    expect(await ensureToken()).toBe('fresh')
  })

  it('retries only once and then reports the 401', async () => {
    storage.setItem('void.operatorToken', 'stale')
    mockFetch((c) => (c.url === '/api/session' ? json(200, { token: 'also-bad' }) : json(401, { detail: 'operator token required' })))
    const err = (await apiPost('/api/control/pause').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(401)
    expect(err.message).toBe('operator token required (401)')
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(2)
    expect(calls.filter((c) => c.url === '/api/session')).toHaveLength(1)
    expect(storage.getItem('void.operatorToken')).toBeNull()
  })

  it('does not retry when no fresh token can be had', async () => {
    storage.setItem('void.operatorToken', 'stale')
    mockFetch((c) => (c.url === '/api/session' ? json(403, { detail: 'same-origin only' }) : json(401, { detail: 'operator token required' })))
    const err = (await apiPost('/api/control/resume').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(401)
    expect(calls.map((c) => c.url)).toEqual(['/api/control/resume', '/api/session'])
  })
})

describe('operator feedback', () => {
  const names: Record<string, string> = { a1: 'Ada', b2: 'Bao' }
  const nameOf = (id: string) => names[id] ?? id

  it('reports what step did', () => {
    expect(describeResult({ tick: 42, day: 2, status: 'running', calls: 5, deaths: ['a1'], births: ['b2', 'c3'] }, nameOf)).toBe(
      'ran tick 42 (day 2): 5 calls, 1 death (Ada), 2 births (Bao, c3)',
    )
    expect(describeResult({ tick: 43, day: 2, status: 'extinct', calls: 1, deaths: [], births: [] }, nameOf)).toBe('ran tick 43 (day 2): 1 call, 0 deaths, 0 births; run is extinct')
  })

  it('reports the loop status pause, resume and speed answer with, not a queue position', () => {
    expect(describeResult({ type: 'status', paused: true, tick_seconds: 0.5, status: 'running' })).toBe('paused · 0.5 s per tick')
    expect(describeResult({ type: 'status', paused: false, tick_seconds: 2, status: 'running' })).toBe('running · 2 s per tick')
    expect(describeResult({ type: 'status', paused: false, tick_seconds: 2, status: 'completed' })).toBe('running · 2 s per tick · run is completed')
  })

  it('says when an enqueued command applies', () => {
    expect(describeResult({ cmd_id: 3, will_apply_at_tick: 18 })).toBe('queued as command 3, applies at tick 18')
  })

  it('turns controls off with a reason once the run is not running', () => {
    expect(controlsBlockedReason('running')).toBeNull()
    expect(controlsBlockedReason('completed')).toBe('run is completed')
    expect(controlsBlockedReason('capped_total')).toBe('run is capped_total')
    expect(controlsBlockedReason(null)).toBe('waiting for the server')
  })
})

describe('form limits', () => {
  // the pydantic bodies the forms post to (void/server/api.py)
  const src = readFileSync(new URL('../../../void/server/api.py', import.meta.url), 'utf8')
  function bounds(cls: string, field: string): Record<string, number> {
    const start = src.indexOf(`class ${cls}(BaseModel):`)
    expect(start, `class ${cls}`).toBeGreaterThanOrEqual(0)
    const block = src.slice(start, src.indexOf('\n\n', start))
    const m = new RegExp(`\\n\\s+${field}:[^\\n]*?Field\\(([^\\n]*)\\)`).exec(block)
    expect(m, `${cls}.${field}`).not.toBeNull()
    const out: Record<string, number> = {}
    for (const kv of m![1]!.matchAll(/(\w+)=(-?\d+(?:\.\d+)?)/g)) out[kv[1]!] = Number(kv[2])
    return out
  }

  it('match the server validation', () => {
    expect(bounds('PostTaskBody', 'title').max_length).toBe(LIMITS.taskTitle)
    expect(bounds('PostTaskBody', 'description').max_length).toBe(LIMITS.taskDescription)
    expect(bounds('PostTaskBody', 'reward_usd')).toMatchObject({ ge: LIMITS.taskReward.min, le: LIMITS.taskReward.max })
    expect(bounds('ArrivalBody', 'name').max_length).toBe(LIMITS.arrivalName)
    expect(bounds('ArrivalBody', 'balance_usd')).toMatchObject({ ge: LIMITS.arrivalBalance.min, le: LIMITS.arrivalBalance.max })
    expect(bounds('EpochBody', 'day').ge).toBe(LIMITS.epochDay.min)
    expect(bounds('EpochBody', 'scarcity')).toMatchObject({ ge: LIMITS.scarcity.min, le: LIMITS.scarcity.max })
    expect(bounds('EpochBody', 'weather_baseline')).toMatchObject({ ge: LIMITS.weather.min, le: LIMITS.weather.max })
    expect(bounds('EpochBody', 'duration_days')).toMatchObject({ ge: LIMITS.durationDays.min, le: LIMITS.durationDays.max })
    expect(bounds('BenefactorBody', 'amount_usd')).toMatchObject({ gt: LIMITS.benefactorAmount.min, le: LIMITS.benefactorAmount.max })
    expect(bounds('SpeedBody', 'tick_seconds')).toMatchObject({ ge: LIMITS.tickSeconds.min, le: LIMITS.tickSeconds.max })
  })
})

describe('fixture mode', () => {
  // FIXTURE_MODE is read when api.ts loads, so each case imports a fresh copy (and the store it reads the run id from)
  // `api` undefined: there is no api.json (404)
  async function fixtureModule(api: unknown, runId: string | null = null) {
    vi.resetModules()
    vi.stubEnv('VITE_FEED', 'fixture')
    mockFetch((c) => {
      if (c.url !== '/fixtures/api.json') return json(404, { detail: 'no server in fixture mode' })
      return api === undefined ? new Response('', { status: 404 }) : json(200, api)
    })
    const mod = await import('./api')
    const { useStore } = await import('../state/store')
    useStore.setState({ runId })
    expect(mod.FIXTURE_MODE).toBe(true)
    return mod
  }

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    vi.resetModules()
  })

  const version = (v: number, tick: number) => ({
    version: v,
    tick,
    source: v === 1 ? 'seed' : 'edit',
    source_agent_id: null,
    parent_version: v === 1 ? null : v - 1,
    similarity_to_parent: null,
    text: `policy v${v}`,
  })
  const event = (i: number) => ({ event_id: `pe${i}`, tick: 100 - i, kind: 'suggested', source_agent_id: 'a1', target_agent_id: 'b 2', excerpt: 'share', similarity: null })
  const sample = {
    run_id: 'run-x',
    agents: {},
    policy: { a1: { agent_id: 'a1', versions: [version(2, 30), version(1, 0)] }, 'b 2': { agent_id: 'b 2', versions: [] } },
    memory_summary: { agents: [{ agent_id: 'a1', name: 'Ada', alive: true, notes: 3, links: 1, bytes: 120, policy_source: 'edit', policy_version: 2 }] },
    practices: { events: [event(0), event(1), event(2)] },
  }

  it('answers the policy, memory summary and practices routes from api.json', async () => {
    const { apiGet } = await fixtureModule(sample, 'run-x')
    expect(await apiGet('/api/agents/a1/policy')).toEqual(sample.policy.a1)
    expect(await apiGet(`/api/agents/${encodeURIComponent('b 2')}/policy`)).toEqual(sample.policy['b 2'])
    expect(await apiGet('/api/memory/summary')).toEqual(sample.memory_summary)
    expect(await apiGet('/api/practices?limit=5000')).toEqual(sample.practices)
    expect(await apiGet('/api/practices')).toEqual(sample.practices)
    // a smaller limit keeps the newest rows, as the route does
    expect(await apiGet('/api/practices?limit=2')).toEqual({ events: [event(0), event(1)] })
    // nothing reached a server: api.json was fetched once
    expect(calls.map((c) => c.url)).toEqual(['/fixtures/api.json'])
  })

  it('answers 404 for an agent without a policy, and when api.json belongs to another run or is missing', async () => {
    const own = await fixtureModule(sample, 'run-x')
    const unknown = (await own.apiGet('/api/agents/zz/policy').catch((e: unknown) => e)) as ApiError
    expect(unknown).toBeInstanceOf(own.ApiError)
    expect(unknown.status).toBe(404)

    const other = await fixtureModule(sample, 'run-y')
    for (const path of ['/api/agents/a1/policy', '/api/memory/summary', '/api/practices']) {
      const e = (await other.apiGet(path).catch((x: unknown) => x)) as ApiError
      expect(e, path).toBeInstanceOf(other.ApiError)
      expect(e.status, path).toBe(404)
    }

    const none = await fixtureModule(undefined, 'run-x')
    for (const path of ['/api/agents/a1/policy', '/api/memory/summary', '/api/practices']) {
      const e = (await none.apiGet(path).catch((x: unknown) => x)) as ApiError
      expect(e, path).toBeInstanceOf(none.ApiError)
      expect(e.status, path).toBe(404)
    }
  })

  it('serves the committed recording: a policy for every agent, the summary and the practices', async () => {
    const committed = JSON.parse(readFileSync(new URL('../../public/fixtures/api.json', import.meta.url), 'utf8')) as {
      run_id: string
      agents: Record<string, unknown>
      policy: Record<string, { agent_id: string; versions: unknown[] }>
      memory_summary: { agents: Array<{ agent_id: string }> }
      practices: { events: unknown[] }
    }
    const { apiGet } = await fixtureModule(committed, committed.run_id)
    const ids = Object.keys(committed.agents).sort()
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) {
      const p = await apiGet<{ agent_id: string; versions: unknown[] }>(`/api/agents/${encodeURIComponent(id)}/policy`)
      expect(p.agent_id).toBe(id)
      expect(p).toEqual(committed.policy[id])
    }
    const summary = await apiGet<{ agents: Array<{ agent_id: string }> }>('/api/memory/summary')
    expect(summary.agents.map((r) => r.agent_id).sort()).toEqual(ids)
    expect(await apiGet('/api/practices?limit=5000')).toEqual(committed.practices)
  })

  it('names the recording command when mock.jsonl is missing', async () => {
    vi.resetModules()
    mockFetch(() => new Response('', { status: 404 }))
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { FIXTURE_COMMAND, startFixture } = await import('./fixture')
    expect(FIXTURE_COMMAND).toBe('void mock-feed --config configs/demo_memory.yaml --ticks 240 --out web/public/fixtures/mock.jsonl')
    const stop = await startFixture()
    stop()
    expect(err).toHaveBeenCalledTimes(1)
    expect(String(err.mock.calls[0]![0])).toContain(`\`${FIXTURE_COMMAND}\``)
    expect(String(err.mock.calls[0]![0])).not.toContain('gen:fixture')
  })
})
