import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../net/api'
import type { DayRow, EventMsg, HelloConfig, MemorySummaryItem, MemorySummaryResponse, RosterAgent, VaultStats } from '../../protocol'
import { MemorySeries } from '../../state/series'
import { useStore } from '../../state/store'
import { EMPTY_TEXT } from '../cards/EmptyState'
import { examGrid, maintenanceDays, MemoryBody, MemoryCard, memoryAgentRows, memoryTotals, pollMemorySummary } from '../cards/MemoryCard'
import { VaultBody, VaultCard } from '../cards/VaultCard'
import { VaultChart, vaultPoints, type VaultPoint } from './VaultChart'

const CONFIG = {
  name: 't',
  seed: 1,
  days: 4,
  ticks_per_day: 24,
  tick_seconds: 0,
  render_delay_ticks: 0,
  world_size: 32,
  population_cap: 12,
  daily_cap_usd: 1,
  total_cap_usd: 5,
  tiers: { sharp: { color: '#3987e5', model: 'm', provider: 'scripted' }, dim: { color: '#d95926', model: 'm', provider: 'scripted' } },
  memory_layer: { enabled: true, policy: false, maintenance: true, exam: true },
} as HelloConfig

const ROSTER: RosterAgent[] = [
  { id: 'a1', name: 'Ada', tier: 'sharp', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' },
  { id: 'a2', name: 'Bo', tier: 'dim', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' },
  { id: 'a3', name: 'Cy', tier: 'dim', generation: 0, parent_id: null, born_tick: 0, died_tick: 40, status: 'bankrupt' },
]

let seq = 0
let run = 0
function ev(kind: string, day: number, agent: string | null, payload: Record<string, unknown>): EventMsg {
  seq++
  return { type: 'event', seq, tick: day * 24, day, kind, agent_id: agent, visibility: 'operator', payload: { agent_id: agent, ...payload } } as EventMsg
}

const maint = (day: number, agent: string, real: number, world: number, payer = agent) => ev('maintenance', day, agent, { payer, real_cost: real, world_cost: world, commands: 2, ok: 2 })
const skip = (day: number, agent: string, reason: string) => ev('maintenance_skipped', day, agent, { reason, payer: agent })
const exam = (day: number, agent: string, kind: string, correct: boolean, abstained = false, failed = false) =>
  ev('exam_answered', day, agent, { kind, correct, abstained, answer: abstained ? null : 'x', ...(failed ? { failed: true } : {}) })

/** Maintenance spend, a skip, and audit answers on days 2 (reported) and 3 (live only). */
const EVENTS: EventMsg[] = [
  maint(2, 'a1', 1500, 3000),
  maint(2, 'a2', 500, 800, 'research_pool'),
  skip(2, 'a3', 'insufficient'),
  exam(2, 'a1', 'node', true),
  exam(2, 'a1', 'balance', false, true),
  exam(2, 'a2', 'node', false),
  exam(2, 'a2', 'heard', false, true, true),
  maint(3, 'a1', 1000, 2000),
  skip(3, 'a2', 'daily_cap'),
  exam(3, 'a1', 'node', true),
  exam(3, 'a2', 'node', true),
]

const vault = (notes: number, bytes: number, rest: Partial<Record<keyof VaultStats, number | null>> = {}): VaultStats =>
  ({ notes, bytes, mean_note_chars: null, link_density: null, orphan_rate: null, index_coverage: null, ...rest }) as unknown as VaultStats

/** Day 1: empty vaults (the server sends null rates without notes). Day 2: notes, links and the day's reports.
 *  Day 3: vaults only (its reports come with the next row). */
const DAYS: DayRow[] = [
  { day: 1, kind: 'day', population: 3, vault: vault(0, 0), vault_by_agent: { a1: vault(0, 0), a2: vault(0, 0) } },
  {
    day: 2,
    kind: 'day',
    population: 2,
    vault: vault(12, 4096, { mean_note_chars: 240.5, link_density: 1.25, orphan_rate: 0.25, index_coverage: 0.5 }),
    vault_by_agent: {
      a1: vault(8, 3000, { mean_note_chars: 260, link_density: 1.5, orphan_rate: 0.125, index_coverage: 0.75 }),
      a2: vault(4, 1096, { mean_note_chars: 200, link_density: 0.75, orphan_rate: 0.5, index_coverage: null }),
    },
    maintenance: { day: 2, tick: 48, agents: 3, called: 2, skipped: { insufficient: 1 }, commands_ok: 4, commands_refused: 0, real_cost: 2000, world_cost: 3800 },
    exam: {
      day: 2,
      asked: 3,
      correct: 1,
      abstained: 1,
      failed: 1,
      by_kind: { node: { asked: 2, correct: 1, abstained: 0 }, balance: { asked: 1, correct: 0, abstained: 1 }, heard: { asked: 0, correct: 0, abstained: 0, failed: 1 } },
    },
  },
  {
    day: 3,
    kind: 'day',
    population: 2,
    vault: vault(20, 8192, { mean_note_chars: 250, link_density: 1.5, orphan_rate: 0.2, index_coverage: 0.6 }),
    vault_by_agent: {
      a1: vault(12, 5000, { mean_note_chars: 255, link_density: 2, orphan_rate: 0.1, index_coverage: 0.8 }),
      a2: vault(8, 3192, { mean_note_chars: 240, link_density: 0.9, orphan_rate: 0.4, index_coverage: null }),
    },
  },
]

/**
 * Feed the store's reducers as the socket would. A server render reads the store's initial state (zustand's server
 * snapshot), so a rendered card sees what the reducers put into the shared series (events and day rows) but not the
 * roster, the config or the selection: those are covered by rendering the card bodies with props.
 */
function setup(opts: { events?: EventMsg[]; days?: DayRow[] } = {}) {
  const s = useStore.getState()
  s.resetRun()
  run++
  s.applyHello({ type: 'hello', run_id: `mem-${run}`, config: CONFIG, tick: 0, day: 0, last_seq: 0, paused: false, tick_seconds: 0, server_ts_ms: 0, status: 'running' })
  s.applyRoster(ROSTER)
  for (const e of opts.events ?? []) s.applyEvent(e)
  for (const d of opts.days ?? []) s.applyDay(d)
}

const ROSTER_BY_ID = new Map(ROSTER.map((a) => [a.id, a]))
const colorOf = (tier: string | null) => (tier === 'sharp' ? '#3987e5' : '#d95926')

const BAD = /NaN|Infinity|undefined|null|\[object/

beforeEach(() => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null, setItem: () => {} } })
})
afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage
  useStore.getState().resetRun()
})

describe('memory card', () => {
  it('renders spend, audit scores and skips from fixture events and day rows, with no NaN', () => {
    setup({ events: EVENTS, days: DAYS })
    const html = renderToStaticMarkup(<MemoryCard />)
    expect(html).toContain('<h2 class="card-title">Memory</h2>')
    expect(html).not.toMatch(BAD)
    expect(html).not.toContain(EMPTY_TEXT)
    // per agent: a1 paid 0.0015 + 0.0010 real, graded 3 (2 correct, 1 abstained); a2 graded 2 (1 correct), one failed
    for (const id of ['a1', 'a2', 'a3']) expect(html).toContain(`>${id}</span>`)
    expect(html).toContain('$0.0025')
    expect(html).toContain('$0.0050')
    expect(html).toContain('2/3 correct, 1 abstained')
    expect(html).toContain('1/2 correct, 0 abstained, 1 failed (ungraded)')
    expect(html).toContain('>67%<')
    expect(html).toContain('>50%<')
    // no summary yet: the share column waits for the decide cost
    expect(html).toContain('needs the decide cost')
    // run totals: 3 of 5 graded items correct
    expect(html).toContain('audit 60%')
    // by kind and day: day 2 from the report, day 3 live
    expect(html).toContain('>day 2</th>')
    expect(html).toContain('>day 3</th>')
    expect(html).toContain('>balance</td>')
    expect(html).toContain('>heard</td>')
    expect(html).toContain('>failed</td>')
    expect(html).toContain('>100%</td>')
    // maintenance: the reason split arrives with the day row; day 3 shows only the live skip
    expect(html).toContain('could not afford')
    expect(html).toContain('run caps')
  })

  it('shows the empty state before any memory activity', () => {
    setup()
    const html = renderToStaticMarkup(<MemoryCard />)
    expect(html).toContain(EMPTY_TEXT)
    expect(html).not.toMatch(BAD)
  })

  it('names agents from the roster, dims the dead and marks the selected row', () => {
    setup({ events: EVENTS, days: DAYS })
    const { series } = useStore.getState()
    const rows = memoryAgentRows(series.memory, ROSTER_BY_ID, null)
    expect(rows.map((r) => r.name)).toEqual(['Ada', 'Bo', 'Cy'])
    const html = renderToStaticMarkup(
      <MemoryBody
        rows={rows}
        totals={memoryTotals(rows, series.memory, null)}
        grid={examGrid(series.memory, series.daily)}
        maintenance={maintenanceDays(series.memory, series.daily)}
        summaryStatus="unavailable"
        selectedId="a2"
        onSelect={() => {}}
        colorOf={colorOf}
      />,
    )
    expect(html).not.toMatch(BAD)
    expect(html).toMatch(/<tr style="background:rgba\(57, 135, 229, 0.16\)"><td[^>]*><span[^>]*background:#d95926"><\/span><span class="wikilink"[^>]*>Bo<\/span>/)
    expect(html).toMatch(/<tr style="opacity:0.55" title="not alive">.*?>Cy<\/span>/)
    expect(html).toContain('which is unavailable')
  })
})

describe('memory rows', () => {
  function series(): MemorySeries {
    const m = new MemorySeries()
    m.onMaintenance('a1', { payer: 'a1', real_cost: 2000, world_cost: 4000 })
    m.onExamAnswered('a1', 2, { kind: 'node', correct: true })
    m.onExamAnswered('a1', 2, { kind: 'node', correct: false, abstained: true })
    m.onMaintenanceSkipped('a2', 2)
    m.onMaintenanceSkipped(null, 2)
    return m
  }
  const roster = new Map(ROSTER.map((a) => [a.id, a]))
  const item = (agent_id: string, over: Partial<MemorySummaryItem>): MemorySummaryItem => ({
    agent_id,
    name: agent_id,
    alive: true,
    notes: 0,
    links: 0,
    bytes: 0,
    policy_source: null,
    policy_version: null,
    maintenance_real_usd: 0,
    maintenance_world_usd: 0,
    decide_real_usd: 0,
    exam_asked: 0,
    exam_correct: 0,
    exam_abstained: 0,
    ...over,
  })

  it('without a summary: live figures, no share', () => {
    const m = series()
    const rows = memoryAgentRows(m, roster, null)
    expect(rows.map((r) => r.id)).toEqual(['a1', 'a2'])
    const a1 = rows[0]!
    expect(a1.maintenanceReal).toBeCloseTo(0.002, 12)
    expect(a1.maintenanceWorld).toBeCloseTo(0.004, 12)
    expect(a1).toMatchObject({ asked: 2, correct: 1, abstained: 1, share: null, decideReal: null })
    expect(a1.accuracy).toBeCloseTo(0.5, 12)
    expect(rows[1]).toMatchObject({ skipped: 1, asked: 0, accuracy: null, abstainRate: null })
    const t = memoryTotals(rows, m, null)
    // the skip without an agent counts in the run total
    expect(t).toMatchObject({ skipped: 2, share: null, asked: 2, correct: 1 })
  })

  it('with a summary: share from its maintenance and decide cost, the larger totals win', () => {
    const m = series()
    const summary = [
      // the summary is behind the live maintenance figure but has graded more items (the page joined late)
      item('a1', { maintenance_real_usd: 0.001, maintenance_world_usd: 0.005, decide_real_usd: 0.003, exam_asked: 4, exam_correct: 3, exam_abstained: 0 }),
      item('a2', { decide_real_usd: 0.01 }),
      // dead and idle: left out of the rows, but its decide cost counts in the run's share
      item('a3', { alive: false, decide_real_usd: 0.006 }),
    ]
    const rows = memoryAgentRows(m, roster, summary)
    expect(rows.map((r) => r.id)).toEqual(['a1', 'a2'])
    const a1 = rows[0]!
    expect(a1.maintenanceReal).toBeCloseTo(0.002, 12)
    expect(a1.maintenanceWorld).toBeCloseTo(0.005, 12)
    expect(a1.share).toBeCloseTo(0.25, 12)
    expect(a1).toMatchObject({ asked: 4, correct: 3, abstained: 0 })
    expect(rows[1]!.share).toBe(0)
    const t = memoryTotals(rows, m, summary)
    expect(t.share).toBeCloseTo(0.001 / 0.02, 12)
  })

  it('a summary with no spend leaves the share empty rather than dividing by zero', () => {
    const rows = memoryAgentRows(new MemorySeries(), roster, [item('a1', {})])
    expect(rows[0]!.share).toBeNull()
    expect(memoryTotals(rows, new MemorySeries(), [item('a1', {})]).share).toBeNull()
  })

  it('renders the share once the summary is in', () => {
    const m = series()
    const summary = [item('a1', { maintenance_real_usd: 0.001, decide_real_usd: 0.003 })]
    const rows = memoryAgentRows(m, roster, summary)
    const html = renderToStaticMarkup(
      <MemoryBody
        rows={rows}
        totals={memoryTotals(rows, m, summary)}
        grid={examGrid(m, [])}
        maintenance={maintenanceDays(m, [])}
        summaryStatus="ok"
        selectedId={null}
        onSelect={() => {}}
        colorOf={() => '#3987e5'}
      />,
    )
    expect(html).toContain('>25%<')
    expect(html).toContain('decide $0.0030')
    expect(html).toContain('from the memory summary')
    expect(html).not.toMatch(BAD)
  })
})

describe('per-day tables', () => {
  it('takes a day from its report when the row has arrived, else from live events', () => {
    const m = new MemorySeries()
    m.onExamAnswered('a1', 2, { kind: 'node', correct: true })
    m.onExamAnswered('a1', 3, { kind: 'claim', correct: false, abstained: true })
    const grid = examGrid(m, DAYS)
    expect(grid.days).toEqual([2, 3])
    expect(grid.kinds).toEqual(['balance', 'claim', 'heard', 'node'])
    expect(grid.byDay.get(2)!.get('node')).toEqual({ asked: 2, correct: 1, abstained: 0, failed: 0 })
    expect(grid.byDay.get(2)!.get('heard')).toEqual({ asked: 0, correct: 0, abstained: 0, failed: 1 })
    expect(grid.byDay.get(3)!.get('claim')).toEqual({ asked: 1, correct: 0, abstained: 1, failed: 0 })
  })

  it('keeps the newest days only', () => {
    const m = new MemorySeries()
    for (let d = 1; d <= 9; d++) {
      m.onExamAnswered('a1', d, { kind: 'node', correct: true })
      m.onMaintenanceSkipped('a1', d)
    }
    expect(examGrid(m, [], 4).days).toEqual([6, 7, 8, 9])
    expect(maintenanceDays(m, [], 3).map((d) => d.day)).toEqual([7, 8, 9])
  })

  it('splits maintenance skips by reason from the day report', () => {
    const m = new MemorySeries()
    m.onMaintenanceSkipped('a3', 2)
    m.onMaintenanceSkipped('a2', 3)
    const rows: DayRow[] = [...DAYS, { day: 4, population: 2, maintenance: { called: 1, skipped: { insufficient: 2, daily_cap: 1, total_cap: 1 } } }]
    expect(maintenanceDays(m, rows)).toEqual([
      { day: 2, called: 2, skipped: 1, couldNotAfford: 1, capped: 0 },
      { day: 3, called: null, skipped: 1, couldNotAfford: null, capped: null },
      { day: 4, called: 1, skipped: 4, couldNotAfford: 2, capped: 2 },
    ])
  })
})

describe('vault card', () => {
  it('plots the population per day from day rows, with no NaN', () => {
    setup({ days: DAYS })
    const html = renderToStaticMarkup(<VaultCard />)
    expect(html).toContain('<h2 class="card-title">Vault</h2>')
    expect(html).not.toMatch(BAD)
    for (const title of ['Notes', 'Bytes', 'Links per note', 'Orphan rate', 'Index coverage', 'Mean note chars']) expect(html).toContain(`<span>${title}</span>`)
    expect(html).toContain('day 3 · 20 notes')
    const latest = (v: string) => `#f1f3fa">${v}</span>`
    for (const v of ['20', '8.0 KB', '1.50', '20%', '60%', '250']) expect(html).toContain(latest(v))
    expect(html).toContain('day 1')
    expect(html).toContain('Select an agent')
  })

  it('adds the selected agent series against the population', () => {
    const html = renderToStaticMarkup(<VaultBody population={vaultPoints(DAYS)} agent={{ id: 'a1', name: 'Ada', color: '#3987e5', points: vaultPoints(DAYS, 'a1') }} />)
    expect(html).not.toMatch(BAD)
    expect(html).toContain('<div class="sub">Ada</div>')
    expect(html).toContain('population (per-note panels)')
    for (const v of ['12', '4.9 KB', '2.00', '10%', '80%', '255']) expect(html).toContain(`#f1f3fa">${v}</span>`)
    // the population reference is dashed and drawn only on the four per-note panels
    expect(html.match(/stroke-dasharray="3 3"/g)?.length).toBe(4 + 1)
  })

  it('says so when the selected agent has no vault figures', () => {
    const html = renderToStaticMarkup(<VaultBody population={vaultPoints(DAYS)} agent={{ id: 'a3', name: 'Cy', color: '#d95926', points: vaultPoints(DAYS, 'a3') }} />)
    expect(html).toContain('no vault figures for Cy yet')
    expect(html).not.toMatch(BAD)
  })

  it('shows the empty state without day rows', () => {
    setup({ events: EVENTS })
    const html = renderToStaticMarkup(<VaultCard />)
    expect(html).toContain(EMPTY_TEXT)
  })
})

describe('vault chart', () => {
  it('reads the population and one agent, keeping nulls', () => {
    const pop = vaultPoints([DAYS[2]!, DAYS[0]!, DAYS[1]!])
    expect(pop.map((p) => p.day)).toEqual([1, 2, 3])
    expect(pop[0]).toMatchObject({ notes: 0, link_density: null, index_coverage: null })
    expect(pop[2]).toMatchObject({ notes: 20, bytes: 8192, link_density: 1.5, index_coverage: 0.6 })
    expect(vaultPoints(DAYS, 'a2').map((p) => p.index_coverage)).toEqual([null, null, null])
    expect(vaultPoints(DAYS, 'nobody')).toEqual([])
    expect(vaultPoints(DAYS, 'constructor')).toEqual([])
  })

  it('draws a single day and all-null panels without NaN', () => {
    const one: VaultPoint[] = [{ day: 5, notes: 3, bytes: 900, mean_note_chars: 80, link_density: 0, orphan_rate: 1, index_coverage: null }]
    const html = renderToStaticMarkup(<VaultChart lines={[{ key: 'p', label: 'population', color: '#3987e5', points: one }]} />)
    expect(html).not.toMatch(BAD)
    expect(html).toContain('no index note yet')
    expect(html).toContain('>900 B<')
    expect(html).toContain('day 5')
  })

  it('breaks the line at a null and dots an isolated point', () => {
    const pts: VaultPoint[] = [1, 2, 3, 4].map((day) => ({
      day,
      notes: day,
      bytes: day * 100,
      mean_note_chars: 50,
      link_density: day === 3 ? null : 1,
      orphan_rate: 0,
      index_coverage: day === 2 ? 0.5 : null,
    }))
    const html = renderToStaticMarkup(<VaultChart lines={[{ key: 'p', label: 'population', color: '#3987e5', points: pts }]} />)
    expect(html).not.toMatch(BAD)
    // links per note: days 1-2 joined, day 4 alone after the gap
    expect(html).toMatch(/<circle cx="[\d.]+" cy="[\d.]+" r="2"/)
  })

  it('says when there is nothing to plot', () => {
    expect(renderToStaticMarkup(<VaultChart lines={[]} />)).toContain('No vault figures yet.')
  })
})

describe('memory summary polling', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  function poll(answer: () => Promise<MemorySummaryResponse>) {
    const paths: string[] = []
    const got: Array<MemorySummaryItem[] | 'fail'> = []
    const get = (path: string) => {
      paths.push(path)
      return answer()
    }
    const stop = pollMemorySummary(
      (items) => got.push(items),
      () => got.push('fail'),
      get,
      1000,
    )
    return { paths, got, stop }
  }

  it('asks at once, then again after each answer, until stopped', async () => {
    vi.useFakeTimers()
    const p = poll(() => Promise.resolve({ agents: [] }))
    await vi.advanceTimersByTimeAsync(0)
    expect(p.paths).toEqual(['/api/memory/summary'])
    expect(p.got).toEqual([[]])
    await vi.advanceTimersByTimeAsync(2000)
    expect(p.paths.length).toBe(3)
    p.stop()
    await vi.advanceTimersByTimeAsync(5000)
    expect(p.paths.length).toBe(3)
  })

  it('stops asking a server without the route, and retries other failures', async () => {
    vi.useFakeTimers()
    const missing = poll(() => Promise.reject(new ApiError(404, 'not found')))
    const flaky = poll(() => Promise.reject(new ApiError(503, 'busy')))
    await vi.advanceTimersByTimeAsync(3500)
    expect(missing.paths.length).toBe(1)
    expect(missing.got).toEqual(['fail'])
    expect(flaky.paths.length).toBe(4)
    flaky.stop()
    missing.stop()
  })

  it('treats a body without an agent list as empty', async () => {
    vi.useFakeTimers()
    const p = poll(() => Promise.resolve({} as MemorySummaryResponse))
    await vi.advanceTimersByTimeAsync(0)
    expect(p.got).toEqual([[]])
    p.stop()
  })
})
