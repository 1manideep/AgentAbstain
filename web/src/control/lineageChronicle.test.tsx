import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../net/api'
import type { MemorySummaryItem, PracticeEventRow, PracticesResponse, TreeNode } from '../protocol'
import { useStore } from '../state/store'
import { INK, SERIES } from './charts/chartTheme'
import {
  BackIssueBody,
  ChronicleBody,
  ChronicleIssue,
  FIRST_DAY,
  FIXTURE_NOTE,
  FOLLOW,
  NO_ISSUE,
  PagerBar,
  pagerReducer,
  pagerView,
  type PagerAction,
  type PagerState,
} from './Chronicle'
import {
  causesFrom,
  causeTitle,
  INHERITED_EDITED_RING,
  inheritedEdited,
  Lineage,
  LineageTree,
  needsPolicyHistory,
  NO_POLICY_COLOR,
  OTHER_SOURCE_COLOR,
  POLICY_SOURCE_COLORS,
  POLICY_SOURCES,
  policyInfo,
  policyNote,
  policySourceColor,
  SourceLegend,
  type PolicyInfo,
} from './Lineage'
import { AdoptionBody, AdoptionCard, ADOPTION_EMPTY, adoptionDays, adoptionTotals, dayOfTick, PRACTICES_PATH, pollPractices } from './cards/AdoptionCard'
import { EMPTY_TEXT } from './cards/EmptyState'

const BAD = /NaN|Infinity|undefined|\[object/

let stored: Record<string, string> = {}
beforeEach(() => {
  stored = {}
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: (k: string) => (k in stored ? stored[k] : null), setItem: (k: string, v: string) => (stored[k] = v) },
  })
})
afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage
  useStore.getState().resetRun()
  vi.useRealTimers()
})

// ------------------------------------------------------------------ lineage

function node(id: string, generation: number, children: TreeNode[] = [], dead = false): TreeNode {
  return { id, name: id.toUpperCase(), tier: 'sharp', generation, status: dead ? 'archived' : 'alive', born_tick: generation * 10, died_tick: dead ? 99 : null, children }
}

function summary(agent_id: string, policy_source: string | null, policy_version: number | null = policy_source ? 1 : null): MemorySummaryItem {
  return {
    agent_id,
    name: agent_id,
    alive: true,
    notes: 0,
    links: 0,
    bytes: 0,
    policy_source,
    policy_version,
    maintenance_real_usd: 0,
    maintenance_world_usd: 0,
    decide_real_usd: 0,
    exam_asked: 0,
    exam_correct: 0,
    exam_abstained: 0,
  }
}

describe('lineage colour by policy source', () => {
  it('maps each of the six sources to its own colour', () => {
    expect(POLICY_SOURCES).toEqual(['seed', 'inherited', 'self_edit', 'adapted', 'mutated', 'adopted'])
    expect(POLICY_SOURCE_COLORS).toEqual({
      seed: INK.muted,
      inherited: SERIES[6],
      self_edit: SERIES[0],
      adapted: SERIES[2],
      mutated: SERIES[1],
      adopted: SERIES[4],
    })
    const colours = POLICY_SOURCES.map((s) => policySourceColor(s))
    expect(colours).toEqual(POLICY_SOURCES.map((s) => POLICY_SOURCE_COLORS[s]))
    expect(new Set(colours).size).toBe(6)
    for (const c of colours) {
      expect(c).not.toBe(NO_POLICY_COLOR)
      expect(c).not.toBe(OTHER_SOURCE_COLOR)
    }
    expect(INHERITED_EDITED_RING).toBe(POLICY_SOURCE_COLORS.inherited)
  })

  it('draws no policy hollow and an unknown source neutral', () => {
    expect(policySourceColor(null)).toBe(NO_POLICY_COLOR)
    expect(policySourceColor(undefined)).toBe(NO_POLICY_COLOR)
    expect(policySourceColor('')).toBe(NO_POLICY_COLOR)
    expect(policySourceColor('crossover')).toBe(OTHER_SOURCE_COLOR)
    expect(policySourceColor('toString')).toBe(OTHER_SOURCE_COLOR)
  })

  it('marks a policy copied from the parent and edited by the agent since', () => {
    const inherited = [{ source: 'inherited' as const }, { source: 'self_edit' as const }]
    const seeded = [{ source: 'seed' as const }, { source: 'self_edit' as const }]
    // adapted is the child's own edit of its inheritance: no history needed
    expect(inheritedEdited('adapted', 1, null)).toBe(true)
    // agent edits of a child need the history to tell an inherited start from a seeded one
    expect(inheritedEdited('self_edit', 1, null)).toBeNull()
    expect(inheritedEdited('self_edit', 1, inherited)).toBe(true)
    expect(inheritedEdited('self_edit', 1, seeded)).toBe(false)
    expect(inheritedEdited('adopted', 2, [{ source: 'mutated' }, { source: 'adopted' }])).toBe(true)
    // founders and arrivals start from a seed
    expect(inheritedEdited('self_edit', 0, null)).toBe(false)
    // the kernel's own copies and seeds are not agent edits
    for (const s of ['seed', 'inherited', 'mutated', null, 'crossover']) expect(inheritedEdited(s, 3, inherited)).toBe(false)
    expect(needsPolicyHistory('self_edit', 1)).toBe(true)
    expect(needsPolicyHistory('adopted', 4)).toBe(true)
    expect(needsPolicyHistory('adapted', 1)).toBe(false)
    expect(needsPolicyHistory('self_edit', 0)).toBe(false)
  })

  it('joins the summary, the tree generations and the loaded histories', () => {
    const roots = [node('a', 0, [node('b', 1, [node('c', 2)]), node('d', 1)])]
    const items = [summary('a', 'self_edit', 3), summary('b', 'self_edit', 2), summary('c', 'adapted', 2), summary('d', 'adopted', 4), summary('e', null)]
    const marks = new Map([['b:2', true]])
    const info = policyInfo(items, roots, marks)
    expect(info.get('a')).toEqual({ source: 'self_edit', version: 3, inheritedEdited: false })
    expect(info.get('b')).toEqual({ source: 'self_edit', version: 2, inheritedEdited: true })
    expect(info.get('c')).toEqual({ source: 'adapted', version: 2, inheritedEdited: true })
    // d's history for version 4 is not loaded yet
    expect(info.get('d')).toEqual({ source: 'adopted', version: 4, inheritedEdited: null })
    expect(info.get('e')).toEqual({ source: null, version: null, inheritedEdited: false })
    // a stale history (an older version) does not answer for the current one
    expect(policyInfo([summary('b', 'self_edit', 3)], roots, marks).get('b')!.inheritedEdited).toBeNull()
    expect(policyInfo(null, roots, marks).size).toBe(0)
  })

  it('renders source colours, the inherited-then-edited ring and causes of death as text', () => {
    const roots = [node('a', 0, [node('b', 1), node('c', 1, [], true)]), node('z', 0, [], true)]
    const policy = new Map<string, PolicyInfo>([
      ['a', { source: 'seed', version: 1, inheritedEdited: false }],
      ['b', { source: 'self_edit', version: 2, inheritedEdited: true }],
      ['c', { source: 'mutated', version: 1, inheritedEdited: false }],
    ])
    const causes = new Map<string, string | null>([
      ['c', 'gate'],
      ['z', null],
    ])
    const html = renderToStaticMarkup(<LineageTree roots={roots} colour="source" tierColor={() => '#123456'} policy={policy} causes={causes} selectedId="b" onSelect={() => {}} />)
    expect(html).not.toMatch(BAD)
    expect(html).not.toContain('#123456')
    expect(html).toContain(`background:${INK.muted}`)
    expect(html).toContain(`background:${SERIES[0]}`)
    expect(html).toContain(`background:${SERIES[1]}`)
    // only b carries the ring and the mark
    expect(html.match(new RegExp(`0 0 0 3px ${INHERITED_EDITED_RING}`, 'g'))!.length).toBe(1)
    expect(html.match(/data-mark="inherited-edited"/g)!.length).toBe(1)
    expect(html).toContain('inherited, edited')
    expect(html).toContain('g1 · self_edit v2')
    // z is not in the summary: hollow, and said so
    expect(html).toContain('data-source="none"')
    expect(html).toContain('policy ?')
    expect(html).toContain('inset 0 0 0 1.5px')
    // archived rows: the graveyard's cause, or that it was not recorded
    expect(html).toContain(` · gate`)
    expect(html).toContain(`title="${causeTitle('gate')}"`)
    expect(html).toContain(' · cause unknown')
    // a living row has no cause
    expect(html.match(/data-cause=/g)!.length).toBe(2)
  })

  it('keeps the tier colours, and shows no cause for a death the graveyard does not list', () => {
    const roots = [node('a', 0, [node('c', 1, [], true)])]
    const html = renderToStaticMarkup(<LineageTree roots={roots} colour="tier" tierColor={() => '#123456'} selectedId={null} onSelect={() => {}} />)
    expect(html).toContain('background:#123456')
    expect(html).toContain('g1 · sharp')
    expect(html).not.toContain('data-source=')
    expect(html).not.toContain('data-cause=')
    expect(html).toContain('✕ 99')
    expect(renderToStaticMarkup(<LineageTree roots={[]} colour="source" tierColor={() => '#000'} selectedId={null} onSelect={() => {}} />)).toContain('No agents yet.')
  })

  it('lists every source, no policy and the ring in the legend', () => {
    const html = renderToStaticMarkup(<SourceLegend />)
    for (const s of POLICY_SOURCES) expect(html).toContain(`data-source="${s}"`)
    expect(html).toContain('self edit')
    expect(html).toContain('no policy')
    expect(html).toContain('inherited, then edited')
  })

  it('reads causes from the graveyard and explains the known ones', () => {
    const m = causesFrom({
      agents: [
        { id: 'x', name: 'X', tier: 't', generation: 0, parent_id: null, born_tick: 0, died_tick: 5, cause: 'overrun', children: 0 },
        { id: 'y', name: 'Y', tier: 't', generation: 1, parent_id: 'x', born_tick: 2, died_tick: 7, cause: null, children: 0 },
      ],
    })
    expect([...m]).toEqual([
      ['x', 'overrun'],
      ['y', null],
    ])
    expect(causesFrom(null).size).toBe(0)
    expect(causesFrom({} as never).size).toBe(0)
    expect(causeTitle('gate')).toContain('afford')
    expect(causeTitle('overrun')).toContain('hold')
    expect(causeTitle('eaten')).toBe('eaten')
    expect(causeTitle(null)).toBe('cause not recorded')
  })

  it('says what the colouring rests on', () => {
    const none = new Map<string, PolicyInfo>([['a', { source: null, version: null, inheritedEdited: false }]])
    const pending = new Map<string, PolicyInfo>([
      ['a', { source: 'self_edit', version: 2, inheritedEdited: null }],
      ['b', { source: 'seed', version: 1, inheritedEdited: false }],
    ])
    const done = new Map<string, PolicyInfo>([['a', { source: 'self_edit', version: 2, inheritedEdited: true }]])
    expect(policyNote('fixture', done, true)).toContain('fixture playback has none')
    expect(policyNote('idle', done, true)).toContain('Loading policy sources')
    expect(policyNote('unavailable', done, true)).toContain('unavailable')
    expect(policyNote('ok', none, false)).toContain('no policy file')
    expect(policyNote('ok', none, true)).toBe('No agent has a policy version yet.')
    expect(policyNote('ok', pending, true)).toContain('1 policy history')
    expect(policyNote('ok', done, true)).toBeNull()
  })

  it('shows the toggle, and in fixture mode an empty state for the sources', () => {
    let html = renderToStaticMarkup(<Lineage fixture />)
    expect(html).toContain('colour by')
    expect(html).toContain('aria-pressed="true">tier</button>')
    expect(html).not.toContain('inherited, then edited')
    stored['void.lineage.colour'] = 'source'
    html = renderToStaticMarkup(<Lineage fixture />)
    expect(html).toContain('aria-pressed="true">policy source</button>')
    expect(html).toContain('inherited, then edited')
    expect(html).toContain('fixture playback has none')
    expect(html).toContain('No agents yet.')
  })
})

// ------------------------------------------------------------------ chronicle

describe('chronicle pager', () => {
  const step = (s: PagerState, a: PagerAction, latest: number | null) => pagerReducer(s, a, latest)

  it('follows the latest issue until told otherwise', () => {
    expect(FIRST_DAY).toBe(1)
    expect(pagerView(FOLLOW, 5)).toEqual({ shown: 5, following: true, canOlder: true, canNewer: false })
    expect(step(FOLLOW, { type: 'newer' }, 5)).toEqual(FOLLOW)
    expect(step(FOLLOW, { type: 'latest' }, 5)).toEqual(FOLLOW)
  })

  it('pages back one day at a time and stops at the first day', () => {
    let s = step(FOLLOW, { type: 'older' }, 5)
    expect(s).toEqual({ day: 4 })
    expect(pagerView(s, 5)).toEqual({ shown: 4, following: false, canOlder: true, canNewer: true })
    for (let i = 0; i < 6; i++) s = step(s, { type: 'older' }, 5)
    expect(s).toEqual({ day: 1 })
    expect(pagerView(s, 5)).toEqual({ shown: 1, following: false, canOlder: false, canNewer: true })
  })

  it('pages forward and follows again on reaching the latest', () => {
    let s: PagerState = { day: 3 }
    s = step(s, { type: 'newer' }, 5)
    expect(s).toEqual({ day: 4 })
    s = step(s, { type: 'newer' }, 5)
    expect(s).toEqual(FOLLOW)
    expect(step({ day: 2 }, { type: 'latest' }, 5)).toEqual(FOLLOW)
  })

  it('holds a back issue while new issues arrive', () => {
    const s = step(FOLLOW, { type: 'older' }, 5)
    expect(pagerView(s, 6)).toEqual({ shown: 4, following: false, canOlder: true, canNewer: true })
    expect(step(s, { type: 'newer' }, 6)).toEqual({ day: 5 })
  })

  it('jumps to a day, clamped to the written ones', () => {
    expect(step(FOLLOW, { type: 'go', day: 3 }, 5)).toEqual({ day: 3 })
    expect(step(FOLLOW, { type: 'go', day: 9 }, 5)).toEqual(FOLLOW)
    expect(step(FOLLOW, { type: 'go', day: 0 }, 5)).toEqual({ day: 1 })
    expect(step({ day: 2 }, { type: 'go', day: Number.NaN }, 5)).toEqual({ day: 2 })
  })

  it('has nothing to page before the first issue, or with one issue', () => {
    expect(step({ day: 3 }, { type: 'older' }, null)).toEqual(FOLLOW)
    expect(pagerView({ day: 3 }, null)).toEqual({ shown: null, following: true, canOlder: false, canNewer: false })
    expect(step(FOLLOW, { type: 'older' }, 1)).toEqual(FOLLOW)
    expect(pagerView(FOLLOW, 1)).toEqual({ shown: 1, following: true, canOlder: false, canNewer: false })
    // a held day past the latest (a shorter run) shows the latest
    expect(pagerView({ day: 9 }, 4)).toEqual({ shown: 4, following: true, canOlder: true, canNewer: false })
  })

  it('renders the pager buttons from the view, all off in fixture playback', () => {
    const disabled = (html: string) => (html.match(/disabled=""/g) ?? []).length
    const live = renderToStaticMarkup(<PagerBar view={pagerView(FOLLOW, 5)} latest={5} onAction={() => {}} fixture={false} />)
    expect(live).toContain('day 5 of 5')
    expect(disabled(live)).toBe(2)
    const back = renderToStaticMarkup(<PagerBar view={pagerView({ day: 2 }, 5)} latest={5} onAction={() => {}} fixture={false} />)
    expect(back).toContain('day 2 of 5')
    expect(disabled(back)).toBe(0)
    const fixture = renderToStaticMarkup(<PagerBar view={pagerView({ day: 2 }, 5)} latest={5} onAction={() => {}} fixture />)
    expect(disabled(fixture)).toBe(3)
    expect(renderToStaticMarkup(<PagerBar view={pagerView(FOLLOW, null)} latest={null} onAction={() => {}} fixture={false} />)).toBe('')
  })

  it('renders issues as plain text, never HTML', () => {
    const html = renderToStaticMarkup(<ChronicleIssue issue={{ day: 3, headline: '<b>Rain</b>', markdown: '**bold** <script>x</script>', rev: 7 }} back={false} />)
    expect(html).toContain('&lt;b&gt;Rain&lt;/b&gt;')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toContain('day 3 · rev 7')
    expect(renderToStaticMarkup(<ChronicleIssue issue={{ day: 2, headline: '', markdown: 'x' }} back />)).toContain('Day 2</h3>')
  })

  it('shows each back-issue state', () => {
    expect(renderToStaticMarkup(<BackIssueBody day={2} state={{ status: 'loading' }} />)).toContain('Loading day 2')
    expect(renderToStaticMarkup(<BackIssueBody day={2} state={{ status: 'missing' }} />)).toContain('No issue was written for day 2.')
    expect(renderToStaticMarkup(<BackIssueBody day={2} state={{ status: 'error', message: 'boom (500)' }} />)).toContain('boom (500)')
    const ok = renderToStaticMarkup(<BackIssueBody day={2} state={{ status: 'ok', issue: { day: 2, headline: 'Old news', markdown: 'body' } }} />)
    expect(ok).toContain('Old news')
    expect(ok).toContain('day 2 · back issue')
  })

  it('composes pager and issue: the latest while following, the back issue while held', () => {
    const latest = { day: 5, headline: 'Today', markdown: 'now', rev: 9 }
    const back = { status: 'ok' as const, issue: { day: 3, headline: 'Then', markdown: 'before' } }
    const following = renderToStaticMarkup(<ChronicleBody latest={latest} view={pagerView(FOLLOW, 5)} back={back} onAction={() => {}} fixture={false} />)
    expect(following).toContain('Today')
    expect(following).not.toContain('Then')
    const held = renderToStaticMarkup(<ChronicleBody latest={latest} view={pagerView({ day: 3 }, 5)} back={back} onAction={() => {}} fixture={false} />)
    expect(held).toContain('Then')
    expect(held).toContain('day 3 of 5')
    expect(held).not.toContain('Today')
    const fixture = renderToStaticMarkup(<ChronicleBody latest={latest} view={pagerView(FOLLOW, 5)} back={back} onAction={() => {}} fixture />)
    expect(fixture).toContain(FIXTURE_NOTE)
    expect(fixture).toContain('Today')
    expect(renderToStaticMarkup(<ChronicleBody latest={null} view={pagerView(FOLLOW, null)} back={back} onAction={() => {}} fixture={false} />)).toContain(NO_ISSUE)
  })
})

// ------------------------------------------------------------------ adoption

let eventSeq = 0
function practice(kind: string, tick: number): PracticeEventRow {
  eventSeq++
  return { event_id: `pe${eventSeq}`, tick, kind, source_agent_id: 'a', target_agent_id: 'b', excerpt: 'share the vein', similarity: kind === 'adopted' ? 0.8 : null }
}

describe('adoption card', () => {
  it('says no practice transmission was recorded once the server answers with no rows', () => {
    const html = renderToStaticMarkup(<AdoptionBody days={[]} status="ok" />)
    expect(html).toContain(ADOPTION_EMPTY)
    expect(ADOPTION_EMPTY).toBe('no practice transmission recorded')
    expect(html).not.toContain('<svg')
  })

  it('shows empty states before the answer, on failure, and in fixture playback', () => {
    expect(renderToStaticMarkup(<AdoptionBody days={[]} status="idle" />)).toContain(EMPTY_TEXT)
    expect(renderToStaticMarkup(<AdoptionBody days={[]} status="unavailable" />)).toContain('practice events unavailable')
    const days = adoptionDays([practice('suggested', 5)], 24)
    const fixture = renderToStaticMarkup(<AdoptionBody days={days} status="ok" fixture />)
    expect(fixture).toContain(ADOPTION_EMPTY)
    expect(fixture).not.toContain('<svg')
    const card = renderToStaticMarkup(<AdoptionCard fixture />)
    expect(card).toContain('<h2 class="card-title">Adoption</h2>')
    expect(card).toContain(ADOPTION_EMPTY)
    // the card before its first answer (not fixture playback)
    expect(renderToStaticMarkup(<AdoptionCard fixture={false} />)).toContain(EMPTY_TEXT)
  })

  it('maps ticks to the clock days', () => {
    expect(dayOfTick(0, 24)).toBe(0)
    expect(dayOfTick(1, 24)).toBe(1)
    expect(dayOfTick(24, 24)).toBe(1)
    expect(dayOfTick(25, 24)).toBe(2)
    expect(dayOfTick(73, 24)).toBe(4)
    expect(dayOfTick(5, 0)).toBe(0)
  })

  it('counts each kind per day from a fixture with rows, filling quiet days', () => {
    // newest first, as GET /api/practices answers
    const rows = [
      practice('dropped', 80),
      practice('persisted', 75),
      practice('adopted', 30),
      practice('suggested', 26),
      practice('weird', 26),
      practice('adopted', 25),
      practice('suggested', 20),
      practice('suggested', 3),
      practice('suggested', 1),
    ]
    const days = adoptionDays(rows, 24)
    expect(days).toEqual([
      { day: 1, suggested: 3, adopted: 0, persisted: 0, dropped: 0 },
      { day: 2, suggested: 1, adopted: 2, persisted: 0, dropped: 0 },
      { day: 3, suggested: 0, adopted: 0, persisted: 0, dropped: 0 },
      { day: 4, suggested: 0, adopted: 0, persisted: 1, dropped: 1 },
    ])
    const t = adoptionTotals(days)
    expect(t).toEqual({ suggested: 4, adopted: 2, persisted: 1, dropped: 1, adoptionRate: 0.5, persistence: 0.5 })
    expect(adoptionTotals([])).toEqual({ suggested: 0, adopted: 0, persisted: 0, dropped: 0, adoptionRate: null, persistence: null })
    expect(adoptionDays([], 24)).toEqual([])

    const html = renderToStaticMarkup(<AdoptionBody days={days} status="ok" />)
    expect(html).not.toMatch(BAD)
    expect(html).not.toContain(ADOPTION_EMPTY)
    expect(html).toContain('<svg')
    for (const k of ['suggested', 'adopted', 'persisted', 'dropped']) expect(html).toContain(`data-kind="${k}"`)
    expect(html).toContain('day 1')
    expect(html).toContain('day 4')
    expect(html).toContain('50%')
    expect(html).not.toContain('Showing the newest')
    expect(renderToStaticMarkup(<AdoptionBody days={days} status="ok" truncated />)).toContain('Showing the newest 5000 events')
    expect(renderToStaticMarkup(<AdoptionBody days={days} status="unavailable" />)).toContain('last rows received')
  })

  it('plots a single day without NaN', () => {
    const html = renderToStaticMarkup(<AdoptionBody days={adoptionDays([practice('suggested', 2)], 24)} status="ok" />)
    expect(html).not.toMatch(BAD)
    expect(html).toContain('<svg')
    expect(html).toContain('—')
  })

  describe('polling', () => {
    function poll(answer: () => Promise<PracticesResponse>) {
      const paths: string[] = []
      const got: Array<PracticeEventRow[] | 'fail'> = []
      const stop = pollPractices(
        (events) => got.push(events),
        () => got.push('fail'),
        (path) => {
          paths.push(path)
          return answer()
        },
        1000,
      )
      return { paths, got, stop }
    }

    it('asks at once, then after each answer, until stopped', async () => {
      vi.useFakeTimers()
      const rows = [practice('suggested', 4)]
      const p = poll(() => Promise.resolve({ events: rows }))
      await vi.advanceTimersByTimeAsync(0)
      expect(p.paths).toEqual([PRACTICES_PATH])
      expect(p.got).toEqual([rows])
      await vi.advanceTimersByTimeAsync(2000)
      expect(p.paths.length).toBe(3)
      p.stop()
      await vi.advanceTimersByTimeAsync(5000)
      expect(p.paths.length).toBe(3)
    })

    it('stops asking a server without the route, retries other failures, and reads a bad body as empty', async () => {
      vi.useFakeTimers()
      const missing = poll(() => Promise.reject(new ApiError(404, 'not found')))
      const flaky = poll(() => Promise.reject(new ApiError(503, 'busy')))
      const odd = poll(() => Promise.resolve({} as PracticesResponse))
      await vi.advanceTimersByTimeAsync(3500)
      expect(missing.paths.length).toBe(1)
      expect(missing.got).toEqual(['fail'])
      expect(flaky.paths.length).toBe(4)
      expect(odd.got[0]).toEqual([])
      for (const p of [missing, flaky, odd]) p.stop()
    })
  })
})
