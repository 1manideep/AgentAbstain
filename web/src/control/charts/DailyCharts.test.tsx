import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { DayRow, HelloConfig } from '../../protocol'
import { useStore, type VoidState } from '../../state/store'
import { DailyCard } from '../cards/DailyCard'
import { EMPTY_TEXT } from '../cards/EmptyState'
import { SpendCard } from '../cards/SpendCard'
import { WealthCard } from '../cards/WealthCard'
import { DailyCharts } from './DailyCharts'

const tierColor = (t: string) => (t === 'genius' ? '#3987e5' : '#d95926')

/** Day rows shaped like void/sim/metrics.py record_day (plus the loop's tracer extras), as in public/fixtures/mock.jsonl. */
const DAYS: DayRow[] = [
  {
    day: 1,
    end_tick: 24,
    kind: 'day',
    births: 10,
    births_by_kind: { fresh: 10 },
    deaths: 0,
    deaths_by_tier: {},
    gossip_transfers: 5,
    gossip_similarity_by_hop: { '1': 0.8909 },
    notes_by_channel: { chronicle: 0, gossip: 5, inherited: 0, observed: 107, operator: 0, tracer: 0 },
    gadgets: { rejected: 2, verified: 2 },
    gadget_use_failure_rate: null,
    tracer_reach: 1,
    tracer_reach_by_channel: { observed: 1 },
    tracer_adopters: 0,
  },
  {
    day: 2,
    end_tick: 48,
    kind: 'day',
    births: 2,
    births_by_kind: { offspring: 1, replacement: 1 },
    deaths: 2,
    deaths_by_tier: { genius: 1, dim: 1 },
    gossip_similarity_by_hop: { '1': 0.8924, '2': 0.7236 },
    notes_by_channel: { chronicle: 6, gossip: 14, inherited: 1, observed: 160, operator: 0, tracer: 0 },
    gadget_use_failure_rate: 0.0,
    tracer_reach: 3,
    tracer_reach_by_channel: { observed: 1, gossip: 2 },
    tracer_adopters: 1,
  },
  {
    day: 3,
    end_tick: 72,
    kind: 'day',
    births: 0,
    births_by_kind: {},
    deaths: 1,
    deaths_by_tier: { dim: 1 },
    gossip_similarity_by_hop: { '1': 0.9194, '2': 0.7886, '3': 0.6603 },
    notes_by_channel: { chronicle: 14, gossip: 30, inherited: 1, observed: 212, operator: 0, tracer: 0 },
    gadget_use_failure_rate: 0.25,
    tracer_reach: 4,
    tracer_reach_by_channel: { observed: 1, gossip: 2, unknown: 1 },
    tracer_adopters: 2,
  },
]

/**
 * Rows from older servers or filtered exports: optional fields missing, null, or of the wrong type. Some values break
 * the DayRow type on purpose, so these stay `unknown` until the render, as a parsed wire row would.
 */
const SPARSE: unknown[] = [
  { day: 0 },
  { day: 1, births: 2 },
  { day: 2, deaths: 1, gossip_similarity_by_hop: { '1': null } },
  { day: 3, notes_by_channel: { observed: 'x', gossip: Number.NaN }, births_by_kind: { fresh: -1 } },
  { day: 5, gadget_use_failure_rate: null, tracer_adopters: 1, gossip_similarity_by_hop: null },
]

const render = (rows: DayRow[]) => renderToStaticMarkup(<DailyCharts rows={rows} tierColor={tierColor} tierOrder={['genius', 'dim']} />)

function expectFinite(html: string): void {
  expect(html).not.toContain('NaN')
  expect(html).not.toContain('Infinity')
  expect(html).not.toContain('undefined')
}

/** `d` attributes of every path. */
const pathDs = (html: string) => [...html.matchAll(/<path d="([^"]*)"/g)].map((m) => m[1]!)

describe('DailyCharts', () => {
  it('plots births and deaths, gossip similarity by hop, notes by channel, gadget failures and the tracer from day rows', () => {
    const html = render(DAYS)
    for (const label of [
      'births by kind and deaths by tier per day',
      'gossip similarity by hop per day',
      'live notes by channel per day',
      'gadget use-failure rate per day',
      'tracer reach by channel and adopters per day',
    ])
      expect(html).toContain(`aria-label="${label}"`)
    // legends: birth kinds and dying tiers; hops; channels that ever held a note; tracer channels and adopters
    for (const item of ['fresh', 'offspring', 'replacement', 'genius', 'dim', 'hop 1', 'hop 2', 'hop 3', 'observed', 'gossip', 'chronicle', 'inherited', 'adopters', 'other'])
      expect(html).toContain(`</span>${item}</span>`)
    // a channel at zero on every day is not drawn
    expect(html).not.toContain('</span>operator</span>')
    expect(html).not.toContain('</span>tracer</span>')
    expect(html).not.toContain('No gossip yet.')
    expect(pathDs(html).filter((d) => d.startsWith('M')).length).toBeGreaterThan(8)
    for (const d of pathDs(html)) expect(d).toMatch(/^M[-\d. LHVQZ]+$/)
    expectFinite(html)
  })

  it('renders rows missing optional fields without NaN', () => {
    const html = render(SPARSE as DayRow[])
    expectFinite(html)
    // births from the bare total, deaths without a tier breakdown
    expect(html).toContain('</span>other</span>')
    expect(html).toContain('</span>unknown tier</span>')
    expect(html).toContain('No gossip yet.')
    expect(html).toContain('No notes yet.')
    expect(html).toContain('No gadget uses yet.')
    // adopters alone still open the tracer chart; one value is a dot, not a path
    expect(html).toContain('aria-label="tracer reach by channel and adopters per day"')
    expect(html).toMatch(/<circle cx="[\d.]+" cy="[\d.]+" r="4"/)
  })

  it('shows empty states and no tracer chart for a lone bare row', () => {
    const html = render([{ day: 4 }])
    expectFinite(html)
    expect(html).toContain('No births or deaths yet.')
    expect(html).toContain('No gossip yet.')
    expect(html).not.toContain('tracer')
  })

  it('clamps out-of-range rates and treats non-finite values as gaps', () => {
    const rows: DayRow[] = [
      { day: 1, gadget_use_failure_rate: 1.5 },
      { day: 2, gadget_use_failure_rate: Number.POSITIVE_INFINITY },
      { day: 3, gadget_use_failure_rate: 0.5 },
      { day: 4, gadget_use_failure_rate: 0.25 },
    ]
    const html = render(rows)
    expectFinite(html)
    // day 1 stands alone (a dot), days 3-4 form one segment
    expect(html).toContain('aria-label="gadget use-failure rate per day"')
    expect(pathDs(html).some((d) => /^M[\d.]+ [\d.]+L[\d.]+ [\d.]+$/.test(d))).toBe(true)
  })

  it('draws hops 1-4 as their own lines and folds deeper hops into one', () => {
    const rows: DayRow[] = [
      { day: 1, gossip_similarity_by_hop: { '1': 0.9, '2': 0.8, '3': 0.7, '4': 0.6, '5': 0.5, '7': 0.3 } },
      { day: 2, gossip_similarity_by_hop: { '1': 0.9, '6': 0.4, x: 0.1 } },
    ]
    const html = render(rows)
    expectFinite(html)
    for (const h of ['hop 1', 'hop 2', 'hop 3', 'hop 4', 'hop 5+']) expect(html).toContain(`</span>${h}</span>`)
    expect(html).not.toContain('hop 6')
    expect(html).not.toContain('hop 7')
    expect(html).not.toContain('hop x')
  })

  it('renders a single full day', () => {
    const html = render([DAYS[1]!])
    expectFinite(html)
    expect(html).toContain('d2')
  })

  const fixture = resolve(import.meta.dirname, '..', '..', '..', 'public', 'fixtures', 'mock.jsonl')
  it.skipIf(!existsSync(fixture))('renders the day rows of the recorded fixture', () => {
    const rows = readFileSync(fixture, 'utf8')
      .split('\n')
      .filter((l) => l.includes('"type":"day"') || l.includes('"type": "day"'))
      .map((l) => (JSON.parse(l) as { row: DayRow }).row)
    expect(rows.length).toBeGreaterThan(0)
    const html = render(rows)
    expectFinite(html)
    expect(html).toContain('aria-label="births by kind and deaths by tier per day"')
  })
})

// ---------------------------------------------------------------- the cards that read the store

const CONFIG = {
  name: 't',
  seed: 1,
  days: 3,
  ticks_per_day: 24,
  tick_seconds: 0,
  render_delay_ticks: 0,
  world_size: 32,
  population_cap: 12,
  daily_cap_usd: 1,
  total_cap_usd: 2,
  tiers: { genius: { color: '#3987e5', model: 'm', provider: 'scripted', rank: 0 }, dim: { color: '#d95926', model: 'm', provider: 'scripted', rank: 1 } },
} as HelloConfig

/**
 * renderToStaticMarkup reads zustand's server snapshot, which is the store's initial state, so values a card reads
 * through a selector are staged there for the render and restored after. (The series object is shared, so the
 * reducers' in-place updates are visible either way.)
 */
function withState<T>(patch: Partial<VoidState>, fn: () => T): T {
  const init = useStore.getInitialState()
  const saved: Partial<VoidState> = {}
  for (const k of Object.keys(patch) as Array<keyof VoidState>) (saved as Record<string, unknown>)[k] = init[k]
  Object.assign(init, patch)
  try {
    return fn()
  } finally {
    Object.assign(init, saved)
  }
}

beforeEach(() => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null, setItem: () => {} } })
})
afterEach(() => {
  useStore.getState().resetRun()
  delete (globalThis as { localStorage?: unknown }).localStorage
})

describe('DailyCard', () => {
  it('shows the empty state before any day row', () => {
    expect(renderToStaticMarkup(<DailyCard />)).toContain(EMPTY_TEXT)
  })

  it('plots the day rows in day order once they arrive', () => {
    const st = useStore.getState()
    st.applyDay(DAYS[2]!)
    st.applyDay(DAYS[0]!)
    st.applyDay(DAYS[1]!)
    const html = withState({ config: CONFIG }, () => renderToStaticMarkup(<DailyCard />))
    expect(html).not.toContain(EMPTY_TEXT)
    // deaths wear the tier colours from hello, in rank order in the legend
    expect(html).toContain('fill="#d95926"')
    expect(html.indexOf('</span>genius</span>')).toBeLessThan(html.indexOf('</span>dim</span>'))
    expect(html).toContain('through day 3')
    expect(html).toContain('aria-label="gossip similarity by hop per day"')
    expectFinite(html)
    // the day axis runs d1, d2, d3 left to right
    const labels = [...html.matchAll(/>d(\d+)</g)].map((m) => Number(m[1]))
    expect(labels.slice(0, 3)).toEqual([1, 2, 3])
  })
})

describe('WealthCard trend', () => {
  it('draws mean balance and gini over time from the tick rings, skipping non-finite samples', () => {
    const { series } = useStore.getState()
    for (let t = 0; t < 30; t++) {
      series.meanBalanceByTick.push(t, t === 7 ? Number.NaN : 1 + t / 10)
      series.giniByTick.push(t, 0.1 + t / 100)
    }
    const html = renderToStaticMarkup(<WealthCard />)
    expect(html).toContain('Mean balance of the living, ticks 0-29')
    expect(html).toContain('aria-label="mean balance over time"')
    expect(html).toContain('aria-label="gini over time"')
    expect(html).toContain('$3.900')
    expect(html).toContain('0.39')
    expectFinite(html)
  })

  it('draws no trend before the first snapshot', () => {
    const html = renderToStaticMarkup(<WealthCard />)
    expect(html).not.toContain('mean balance over time')
    expect(html).not.toContain('No calls yet.')
  })
})

describe('SpendCard house wallet', () => {
  it('shows the house wallet under the spawn pool, on the same scale', () => {
    const html = withState({ world: { ...useStore.getState().world, spawnPool: 2, houseUsd: 7.25 } }, () => renderToStaticMarkup(<SpendCard />))
    expect(html).toContain('House wallet')
    expect(html).toContain('$7.250')
    expect(html.indexOf('Spawn pool')).toBeLessThan(html.indexOf('House wallet'))
    expect(html).toContain('aria-label="Spawn pool"')
    // both kernel wallets share max = max(spawn pool, house, 5)
    expect(html.match(/aria-valuemax="7.25"/g)?.length).toBe(2)
    expect(html).toContain('width:100%')
    expectFinite(html)
  })
})
