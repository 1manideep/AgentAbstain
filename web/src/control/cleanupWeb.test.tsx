/**
 * Web follow-ups from the wave reviews: day rows without `population`, labelled sparklines, presentation classes in
 * index.css, the render delay from the fixture's hello, the degradation/epochs model module, and fixture playback
 * asking the api layer for the policy, memory summary and practices reads.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type FixtureApi } from '../net/api'
import { startFixture } from '../net/fixture'
import { ingest } from '../net/ingest'
import type { CallRecord, DayRow, EventMsg, HelloConfig, HelloMsg, MemorySummaryItem, PolicyVersion, PracticeEventRow } from '../protocol'
import { history } from '../state/history'
import { STRESS_BINS } from '../state/series'
import { useStore } from '../state/store'
import { CallCostSplit, Persona } from './AgentPanel'
import { practicesUnrecorded } from './cards/AdoptionCard'
import { WealthCard } from './cards/WealthCard'
import * as model from './charts/degradationEpochsModel'
import * as degradationChart from './charts/DegradationVsStress'
import * as epochsChart from './charts/EpochsStrip'
import { Sparkline } from './charts/Sparkline'
import { POLICY_SOURCES } from './Lineage'
import { fetchPolicyHistory, FIXTURE_NO_POLICY, PolicyHistoryList, PolicyHistoryView, type PolicyLoad } from './PolicyHistory'

const CONFIG: HelloConfig = {
  name: 't',
  seed: 1,
  days: 3,
  ticks_per_day: 24,
  tick_seconds: 0,
  render_delay_ticks: 0,
  world_size: 32,
  population_cap: 8,
  daily_cap_usd: 1,
  total_cap_usd: 2,
  tiers: { sharp: { color: '#3987e5', model: 'm', provider: 'scripted', rank: 0 } },
  memory_layer: { enabled: true, policy: true, maintenance: true, exam: true },
}

const hello = (runId: string, renderDelayTicks: number): HelloMsg => ({
  type: 'hello',
  run_id: runId,
  config: { ...CONFIG, render_delay_ticks: renderDelayTicks },
  tick: 0,
  day: 0,
  last_seq: 0,
  paused: false,
  tick_seconds: 0,
  server_ts_ms: 0,
  status: 'running',
})

const V1: PolicyVersion = { version: 1, tick: 0, source: 'seed', source_agent_id: null, parent_version: null, similarity_to_parent: null, text: '- keep notes short' }
const V2: PolicyVersion = { version: 2, tick: 30, source: 'self_edit', source_agent_id: null, parent_version: 1, similarity_to_parent: 0.5, text: '- keep notes brief' }

/** localStorage stand-in (the test environment is node): the lineage panel reads its colouring from it. */
let stored: Record<string, string> = {}

beforeEach(() => {
  stored = {}
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: (k: string) => stored[k] ?? null, setItem: (k: string, v: string) => void (stored[k] = v) },
  })
})
afterEach(() => {
  useStore.getState().resetRun()
  delete (globalThis as { localStorage?: unknown }).localStorage
})

// ------------------------------------------------------------------ (1) day rows

describe('day rows', () => {
  it('type a record_day row, which carries population_end and not population', () => {
    // tsc -b checks this file: the annotation alone is the test that DayRow no longer requires `population`
    const row: DayRow = { day: 2, kind: 'day', end_tick: 48, births: 1, deaths: 0, population_end: 7 }
    expect(row.population).toBeUndefined()
    expect(row.population_end).toBe(7)
  })
})

// ------------------------------------------------------------------ (2) sparkline labels

describe('sparkline labels', () => {
  it('keeps the call-history name and empty text by default', () => {
    expect(renderToStaticMarkup(<Sparkline values={[]} />)).toBe('<div class="muted small">No calls yet.</div>')
    expect(renderToStaticMarkup(<Sparkline values={[1, 2]} />)).toContain('role="img" aria-label="call history"')
  })

  it('takes an accessible name and an empty text', () => {
    expect(renderToStaticMarkup(<Sparkline values={[1, 2]} ariaLabel="gini over time" />)).toContain('role="img" aria-label="gini over time"')
    expect(renderToStaticMarkup(<Sparkline values={[Number.NaN]} emptyText="No gini yet." />)).toBe('<div class="muted small">No gini yet.</div>')
  })

  it('names the wealth trend lines on the svg itself, and says which one has no value', () => {
    const { series } = useStore.getState()
    for (let t = 0; t < 6; t++) {
      series.meanBalanceByTick.push(t, Number.NaN)
      series.giniByTick.push(t, 0.2 + t / 100)
    }
    const html = renderToStaticMarkup(<WealthCard />)
    expect(html).not.toContain('role="figure"')
    expect(html).toContain('<svg class="sparkline" viewBox="0 0 220 40" width="100%" height="40" role="img" aria-label="gini over time">')
    expect(html).not.toContain('aria-label="mean balance over time"')
    expect(html).toContain('No mean balance yet.')
    expect(html).not.toContain('No calls yet.')
  })
})

// ------------------------------------------------------------------ (3) presentation classes

const CSS = readFileSync(new URL('../index.css', import.meta.url), 'utf8')
const ROOT_TOKENS = new Set([...(/:root \{([^}]*)\}/.exec(CSS)?.[1] ?? '').matchAll(/(--[a-z0-9-]+):/g)].map((m) => m[1]!))

/** The declarations of the top-level rule `selector { ... }` in index.css. */
function rule(selector: string): Record<string, string> {
  const at = CSS.indexOf(`\n${selector} {\n`)
  expect(at, selector).toBeGreaterThanOrEqual(0)
  const body = CSS.slice(CSS.indexOf('{', at) + 1, CSS.indexOf('\n}', at))
  return Object.fromEntries(
    body
      .split(';')
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => [d.slice(0, d.indexOf(':')).trim(), d.slice(d.indexOf(':') + 1).trim()]),
  )
}

/** Colours in the new rules are theme tokens, never literals. */
function expectTokenColours(decl: Record<string, string>, selector: string): void {
  for (const prop of ['color', 'background']) {
    const v = decl[prop]
    if (v === undefined) continue
    const token = /^var\((--[a-z0-9-]+)\)$/.exec(v)?.[1]
    expect(token, `${selector} ${prop}`).toBeDefined()
    expect(ROOT_TOKENS.has(token!), `${selector} ${prop}: ${token}`).toBe(true)
  }
}

function call(purpose: string, real: number, world: number): CallRecord {
  return { tick: 1, real_cost_usd: real, world_cost_usd: world, latency_ms: null, text_coherence: null, invalid_action: 0, degenerate_induced: 0, purpose } as CallRecord
}

describe('presentation classes', () => {
  it('colours each policy source tag with a theme token; an unknown source gets the plain tag', () => {
    expect(ROOT_TOKENS.size).toBeGreaterThan(10)
    for (const s of POLICY_SOURCES) {
      const decl = rule(`.tag.src-${s}`)
      expect(decl.color, s).toBeDefined()
      expectTokenColours(decl, s)
    }
    const odd = { ...V1, version: 9, source: 'x" onclick="y' } as unknown as PolicyVersion
    const versions: PolicyVersion[] = [...POLICY_SOURCES.map((source, i) => ({ ...V1, version: i + 1, source })), odd]
    const html = renderToStaticMarkup(<PolicyHistoryList versions={versions} />)
    for (const s of POLICY_SOURCES) expect(html).toContain(`<span class="tag src-${s}" data-source="${s}">${s}</span>`)
    // the server's text never becomes a class name: it is only escaped data
    expect(html).toContain('<span class="tag src-other" data-source="x&quot; onclick=&quot;y">x&quot; onclick=&quot;y</span>')
  })

  it('lays out the cost-by-purpose table from classes', () => {
    for (const sel of ['.cost-split', '.cost-split .cost-name', '.cost-split .cost-num']) expectTokenColours(rule(sel), sel)
    expect(rule('.cost-split .cost-num')['text-align']).toBe('right')
    const html = renderToStaticMarkup(<CallCostSplit calls={[call('decide', 0.01, 0.02), call('maintain', 0.002, 0.004)]} />)
    expect(html).not.toContain('style=')
    expect(html).toContain('<table class="cost-split small">')
    expect(html).toContain('<th class="cost-name">last 2 calls</th>')
    expect(html).toContain('<tr data-purpose="maintain"><td class="cost-name">maintain</td><td class="cost-num tnum">1</td>')
  })

  it('draws the persona from classes; only the bar widths stay inline', () => {
    for (const sel of ['.motto', '.traits', '.trait-bar', '.trait-fill', '.trait-value']) expectTokenColours(rule(sel), sel)
    expect(rule('.traits').display).toBe('grid')
    const html = renderToStaticMarkup(<Persona motto="Share what pays." traits={{ greed: 0.25, honesty: 1 }} />)
    expect(html).toContain('<p class="motto">“Share what pays.”</p>')
    expect(html).toContain('<div class="traits">')
    expect(html).toContain('<span class="trait-bar"><span class="trait-fill" data-trait="greed"')
    expect(html).toContain('<span class="trait-value tnum small">0.25</span>')
    // every inline style is a fill's geometry: no colour, no layout of its own
    const styles = [...html.matchAll(/style="([^"]*)"/g)].map((m) => m[1])
    expect(styles).toEqual(['display:block;height:100%;width:25%', 'display:block;height:100%;width:100%'])
  })

  it('leaves no inline style in the policy history and no HTML injection anywhere', () => {
    const src = fileURLToPath(new URL('..', import.meta.url))
    expect(readFileSync(join(src, 'control', 'PolicyHistory.tsx'), 'utf8')).not.toContain('style=')
    const files = readdirSync(src, { recursive: true, encoding: 'utf8' }).filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
    expect(files.length).toBeGreaterThan(20)
    for (const f of files) expect(readFileSync(join(src, f), 'utf8'), f).not.toContain('dangerouslySetInnerHTML')
  })
})

// ------------------------------------------------------------------ (4) render delay

describe('render delay from hello', () => {
  afterEach(() => {
    history.setRenderDelayTicks(0)
    vi.unstubAllGlobals()
  })

  it('is applied by ingest, the path the WebSocket client and the fixture replayer share', () => {
    ingest(hello('run-rd', 4))
    expect(history.renderDelayTicks).toBe(4)
    ingest(hello('run-rd', 0))
    expect(history.renderDelayTicks).toBe(0)
  })

  it("comes from the recording's hello in fixture playback", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(hello('run-fx-delay', 6)) + '\n')),
    )
    const stop = await startFixture()
    stop()
    expect(history.renderDelayTicks).toBe(6)
  })
})

// ------------------------------------------------------------------ (5) degradation / epochs model

describe('degradation and epochs model', () => {
  it('computes the chart data outside the component modules', () => {
    expect(model.calibrationLabel(null)).toBeNull()
    expect(model.calibrationLabel({ degeneration_mode: 'observe' })).toBeNull()
    expect(model.calibrationLabel({})).toBe(model.CALIBRATION_LABEL)
    expect(model.dayStep(45)).toBe(5)
    expect(model.epochBlocks([], { active: null, scheduled: [{ id: 'e1', kind: 'storm', day: 3, duration_days: 2 }] })).toMatchObject([
      { id: 'e1', kind: 'storm', startDay: 3, endDay: 5, state: 'scheduled', lane: 0 },
    ])
    expect(model.degradationView({ degradationBins: new Map(), coherenceByTierByStressBin: new Map() }, () => '#000')).toEqual({ source: 'rows', bins: STRESS_BINS, curves: [], total: 0 })
    const curve = { tier: 'sharp', color: '#000', coherence: [0.5], invalid: [0], n: [2], scored: [1] }
    expect(model.pointLabel(curve, 0, 'coherence', 'calls')).toBe('sharp · 1 scored of 2 calls')
    expect(model.pointLabel(curve, 0, 'invalid', 'rows')).toBe('sharp · 2 rows')
  })

  it('is what the chart modules still re-export for older imports', () => {
    expect(degradationChart.calibrationLabel).toBe(model.calibrationLabel)
    expect(degradationChart.degradationView).toBe(model.degradationView)
    expect(degradationChart.pointLabel).toBe(model.pointLabel)
    expect(degradationChart.CALIBRATION_LABEL).toBe(model.CALIBRATION_LABEL)
    expect(epochsChart.epochBlocks).toBe(model.epochBlocks)
    expect(epochsChart.dayStep).toBe(model.dayStep)
    expect(epochsChart.MAX_EPOCH_LANES).toBe(model.MAX_EPOCH_LANES)
  })
})

// ------------------------------------------------------------------ (6) fixture playback asks the api layer

const SUMMARY: MemorySummaryItem = {
  agent_id: 'a1',
  name: 'Ada',
  alive: true,
  notes: 3,
  links: 1,
  bytes: 120,
  policy_source: 'self_edit',
  policy_version: 2,
  maintenance_real_usd: 0.002,
  maintenance_world_usd: 0.004,
  decide_real_usd: 0.018,
  exam_asked: 0,
  exam_correct: 0,
  exam_abstained: 0,
}

const PRACTICES: PracticeEventRow[] = [
  { event_id: 'pe2', tick: 30, kind: 'adopted', source_agent_id: 'a1', target_agent_id: 'a2', excerpt: 'share the vein', similarity: 0.8 },
  { event_id: 'pe1', tick: 26, kind: 'suggested', source_agent_id: 'a1', target_agent_id: 'a2', excerpt: 'share the vein', similarity: null },
]

const API: FixtureApi = {
  run_id: 'run-fx',
  policy: { a1: { agent_id: 'a1', versions: [V2, V1] } },
  memory_summary: { agents: [SUMMARY] },
  practices: { events: PRACTICES },
}

const maintenance = (agent: string): EventMsg => ({
  type: 'event',
  seq: 1,
  tick: 30,
  day: 2,
  kind: 'maintenance',
  agent_id: agent,
  visibility: 'operator',
  payload: { agent_id: agent, payer: agent, real_cost: 1500, world_cost: 3000, commands: 2, ok: 2 },
})

/**
 * Fresh copies of the views in fixture mode: FIXTURE_MODE is read when net/api.ts loads, so the modules are imported
 * again with VITE_FEED=fixture and a fetch that serves `api` as /fixtures/api.json (nothing else answers).
 */
async function fixtureFeed(api: FixtureApi | undefined) {
  vi.resetModules()
  vi.stubEnv('VITE_FEED', 'fixture')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === '/fixtures/api.json' && api !== undefined
        ? new Response(JSON.stringify(api), { status: 200, headers: { 'Content-Type': 'application/json' } })
        : new Response('', { status: 404 }),
    ),
  )
  const net = await import('../net/api')
  expect(net.FIXTURE_MODE).toBe(true)
  const store = await import('../state/store')
  store.useStore.setState({ runId: 'run-fx' })
  return {
    store,
    policy: await import('./PolicyHistory'),
    lineage: await import('./Lineage'),
    memory: await import('./cards/MemoryCard'),
    adoption: await import('./cards/AdoptionCard'),
    empty: await import('./cards/EmptyState'),
  }
}

/** The first answer of a poll (onOk, onFail), then stop it. */
async function firstAnswer<T>(poll: (onOk: (v: T) => void, onFail: () => void) => () => void): Promise<T | 'failed'> {
  let stop = () => {}
  const got = await new Promise<T | 'failed'>((resolve) => {
    stop = poll(resolve, () => resolve('failed'))
  })
  stop()
  return got
}

describe('fixture playback asks the api layer', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it("policy history: fetches the recorded versions; the fixture empty state only for an agent the recording lacks", async () => {
    const { policy } = await fixtureFeed(API)
    const found = await policy.fetchPolicyHistory('a1')
    expect(found).toEqual({ id: 'a1', versions: [V2, V1], missing: false, err: null })
    const lacking = await policy.fetchPolicyHistory('zz')
    expect(lacking).toEqual({ id: 'zz', versions: [], missing: true, err: null })
    // the panel no longer short-circuits in fixture playback: its first paint waits for the answer
    const first = renderToStaticMarkup(<policy.PolicyHistory agentId="a1" />)
    expect(first).toContain('loading')
    expect(first).not.toContain(FIXTURE_NO_POLICY)
    // the answers as the panel shows them
    const shown = renderToStaticMarkup(<policy.PolicyHistoryView load={found} />)
    expect(shown).toContain('data-version="2"')
    expect(shown).toContain('<span class="tag src-self_edit" data-source="self_edit">self_edit</span>')
    expect(renderToStaticMarkup(<policy.PolicyHistoryView load={lacking} />)).toContain(FIXTURE_NO_POLICY)
    // the explicit override still answers without asking
    expect(renderToStaticMarkup(<policy.PolicyHistory agentId="a1" fixture />)).toContain(FIXTURE_NO_POLICY)
  })

  it('policy history live: a 404 is no policy yet, any other failure is an error', async () => {
    const missing: PolicyLoad = await fetchPolicyHistory('a1', () => Promise.reject(new ApiError(404, 'gone')))
    expect(missing).toEqual({ id: 'a1', versions: [], missing: true, err: null })
    expect(renderToStaticMarkup(<PolicyHistoryView load={missing} fixtureMode={false} />)).toContain('No policy versions recorded.')
    const broken = await fetchPolicyHistory('a1', () => Promise.reject(new ApiError(500, 'boom (500)')))
    expect(broken).toEqual({ id: 'a1', versions: null, missing: false, err: 'boom (500)' })
    expect(renderToStaticMarkup(<PolicyHistoryView load={broken} fixtureMode={false} />)).toContain('<div class="error small">boom (500)</div>')
    expect(renderToStaticMarkup(<PolicyHistoryView load={null} />)).toContain('loading')
  })

  it('lineage: asks for the policy sources, and says the playback has none only when told to', async () => {
    stored['void.lineage.colour'] = 'source'
    const { lineage, memory } = await fixtureFeed(API)
    const html = renderToStaticMarkup(<lineage.Lineage />)
    expect(html).toContain('Loading policy sources...')
    expect(html).not.toContain('fixture playback has none')
    expect(renderToStaticMarkup(<lineage.Lineage fixture />)).toContain('fixture playback has none')
    // the summary poll the panel (and the memory card) runs answers from the recording
    expect(await firstAnswer(memory.pollMemorySummary)).toEqual([SUMMARY])
  })

  it('lineage: a recording without the summary fails the poll, and nothing but api.json is fetched', async () => {
    const { memory } = await fixtureFeed({ run_id: 'run-fx' })
    const fetchMock = vi.mocked(fetch)
    expect(await firstAnswer(memory.pollMemorySummary)).toBe('failed')
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(['/fixtures/api.json'])
  })

  it('memory card: the share waits for the recorded summary instead of calling it unavailable', async () => {
    const { store, memory } = await fixtureFeed(API)
    const s = store.useStore.getState()
    s.applyHello(hello('run-fx', 0))
    s.applyEvent(maintenance('a1'))
    const html = renderToStaticMarkup(<memory.MemoryCard />)
    expect(html).toContain('>a1</span>')
    expect(html).toContain('Share needs the decide cost from /api/memory/summary (loading).')
    expect(html).not.toContain('which is unavailable')
  })

  it('adoption card: asks for the recorded practices; the fixture empty state stays for a recording without them', async () => {
    const { adoption, empty } = await fixtureFeed(API)
    const html = renderToStaticMarkup(<adoption.AdoptionCard />)
    expect(html).toContain(empty.EMPTY_TEXT)
    expect(html).not.toContain(adoption.ADOPTION_EMPTY)
    expect(renderToStaticMarkup(<adoption.AdoptionCard fixture />)).toContain(adoption.ADOPTION_EMPTY)
    expect(await firstAnswer(adoption.pollPractices)).toEqual(PRACTICES)
    // what the recording's rows draw
    const body = renderToStaticMarkup(<adoption.AdoptionBody days={adoption.adoptionDays(PRACTICES, 24)} status="ok" />)
    expect(body).toContain('aria-label="practice events per day by kind"')
  })

  it('adoption card: which answers show the fixture empty state', () => {
    const row = PRACTICES[0]!
    expect(practicesUnrecorded(true, false, 'ok', [row])).toBe(true) // told to
    expect(practicesUnrecorded(false, true, 'unavailable', null)).toBe(true) // the recording answered 404
    expect(practicesUnrecorded(false, true, 'unavailable', [row])).toBe(false) // a refresh failed after rows arrived
    expect(practicesUnrecorded(false, true, 'idle', null)).toBe(false) // still asking
    expect(practicesUnrecorded(false, true, 'ok', [])).toBe(false) // recorded, no rows: the card's own empty state
    expect(practicesUnrecorded(false, false, 'unavailable', null)).toBe(false) // live: unavailable is its own state
  })
})
