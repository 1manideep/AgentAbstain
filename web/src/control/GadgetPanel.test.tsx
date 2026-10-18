import { isValidElement, type MouseEvent, type ReactElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DayRow, EventMsg, GadgetItem, GadgetsMsg, RosterAgent } from '../protocol'
import { EventRing } from '../state/events'
import { useStore } from '../state/store'
import { dailyFailureRate, GadgetsCard, MAX_PROPOSALS, ProposalLog, proposalSummary } from './cards/GadgetsCard'
import { EMPTY_TEXT } from './cards/EmptyState'
import {
  COUNTS_STALE_TITLE,
  freshestRegistry,
  GadgetDetails,
  gadgetFailureRate,
  GadgetPanel,
  refreshGadgetCounts,
  REFRESH_MS,
  RegistryRefresh,
  selectGadget,
  startRegistryRefresh,
  useGadgetCounts,
  useGadgetSelection,
} from './GadgetPanel'
import { AgentLink } from './PolicyHistory'

/** A gadget as the server sends it with every wave-1 field filled (void/sim/snapshot.py gadget_item). */
const LAMP: GadgetItem = {
  id: 'gd_lamp',
  name: 'beacon-ada',
  owner: 'ag_ada',
  x: 4.25,
  y: -3.5,
  render: { shape: 'pyramid', color: '#ffcc33', scale: 1.2, label: 'beacon', rotation_deg: 0, height_offset: 0 },
  uses: 3,
  effect_kind: 'forage_bonus',
  effect_value: 0.2,
  purpose: 'find food faster near the ring',
  verified_without_tests: true,
  failures: 2,
  created_tick: 40,
}

/** The same gadget from a server older than the effect fields. */
const OLD: GadgetItem = { id: 'gd_old', name: 'horn-bo', owner: 'ag_bo', x: 0, y: 0, render: { shape: 'cylinder', color: 'nope', scale: 1, label: 'horn' }, uses: 0 }

const ROSTER = [
  { id: 'ag_ada', name: 'Ada', tier: 'scripted', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' },
  { id: 'ag_bo', name: 'Bo', tier: 'scripted', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' },
] as RosterAgent[]

let seq = 0
function gateEvent(kind: 'gadget_verified' | 'gadget_rejected', tick: number, payload: Record<string, unknown>): EventMsg {
  return { type: 'event', seq: ++seq, tick, day: 1, agent_id: String(payload.owner ?? ''), visibility: 'public', kind, payload } as EventMsg
}
function otherEvent(tick: number): EventMsg {
  return { type: 'event', seq: ++seq, tick, day: 1, agent_id: null, visibility: 'public', kind: 'move', payload: {} } as EventMsg
}

/** Every element in a rendered tree (function components are not expanded). */
function elements(node: ReactNode): ReactElement[] {
  const out: ReactElement[] = []
  const walk = (n: ReactNode) => {
    if (Array.isArray(n)) n.forEach(walk)
    else if (isValidElement(n)) {
      out.push(n)
      walk((n.props as { children?: ReactNode }).children)
    }
  }
  walk(node)
  return out
}

/*
 * Rendered to a string, a zustand hook reads its store's *initial* state (the server snapshot), so a connected
 * component would never see what a test put in the store. `ssr` copies the current state over the initial one for
 * the render; afterEach puts the pristine initial state back.
 */
const PRISTINE_STORE = { ...useStore.getInitialState() }
const PRISTINE_SELECTION = { ...useGadgetSelection.getInitialState() }
const PRISTINE_COUNTS = { ...useGadgetCounts.getInitialState() }
function ssr(node: ReactElement): string {
  Object.assign(useStore.getInitialState(), useStore.getState())
  Object.assign(useGadgetSelection.getInitialState(), useGadgetSelection.getState())
  Object.assign(useGadgetCounts.getInitialState(), useGadgetCounts.getState())
  return renderToStaticMarkup(node)
}

/** The newest snapshot tick the client has (what applySnapshot commits into world.tick). */
function atTick(tick: number): void {
  useStore.setState({ world: { ...useStore.getState().world, tick } })
}

beforeEach(() => {
  seq = 0
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null, setItem: () => {} } })
  useStore.getState().resetRun()
  useStore.setState({ runId: null })
  useGadgetSelection.setState({ id: null, runId: null })
  useGadgetCounts.setState({ fetched: null, failed: false })
})
afterEach(() => {
  vi.useRealTimers()
  delete (globalThis as { localStorage?: unknown }).localStorage
  Object.assign(useStore.getInitialState(), PRISTINE_STORE)
  Object.assign(useGadgetSelection.getInitialState(), PRISTINE_SELECTION)
  Object.assign(useGadgetCounts.getInitialState(), PRISTINE_COUNTS)
})

describe('gadget inspector', () => {
  it('renders a fixture gadget with owner, effect, purpose, uses, failures and the untested flag', () => {
    const html = renderToStaticMarkup(<GadgetDetails gadget={LAMP} ownerName="Ada" onSelectOwner={() => {}} />)
    expect(html).toContain('data-gadget-id="gd_lamp"')
    expect(html).toContain('>beacon-ada</span>')
    expect(html).toContain('pyramid · beacon')
    // the owner is a clickable agent link
    expect(html).toMatch(/<span class="wikilink" role="button" tabindex="0" data-agent-id="ag_ada">Ada<\/span>/)
    expect(html).toMatch(/data-field="effect">forage_bonus 0\.20</)
    expect(html).toContain('find food faster near the ring')
    expect(html).toMatch(/data-field="uses" class="tnum">3</)
    expect(html).toMatch(/data-field="failures" class="tnum">2<span class="muted"> · 40% of uses failed<\/span>/)
    expect(html).toContain('data-flag="verified_without_tests"')
    expect(html).toContain('verified without tests (gate off)')
    expect(html).toContain('tick 40')
    expect(html).toContain('4.3, -3.5')
    expect(html).not.toContain('NaN')
  })

  it('shows a gadget that passed its tests as verified', () => {
    const html = renderToStaticMarkup(<GadgetDetails gadget={{ ...LAMP, verified_without_tests: false, failures: 0, uses: 0 }} ownerName="Ada" />)
    expect(html).toContain('<span class="tag approved">verified</span>')
    expect(html).toContain('tests passed at the gate')
    expect(html).not.toContain('data-flag="verified_without_tests"')
    expect(html).not.toContain('of uses failed')
    // without a selection action the owner is plain text
    expect(html).toContain('<span data-agent-id="ag_ada">Ada</span>')
  })

  it('reads fields an older server does not send as not sent, never as zero', () => {
    const html = renderToStaticMarkup(<GadgetDetails gadget={OLD} ownerName="Bo" />)
    for (const field of ['effect', 'purpose', 'failures', 'verified_without_tests']) {
      expect(html).toMatch(new RegExp(`data-field="${field}"[^>]*><span class="muted">not sent by this server</span>`))
    }
    // a malformed colour falls back to the neutral swatch
    expect(html).toContain('background:#8b93a7')
    expect(html).not.toContain('NaN')
  })

  it('selects the owner through the link it renders', () => {
    const picked: string[] = []
    const inner = (GadgetDetails as unknown as { type: (p: Parameters<typeof GadgetDetails>[0]) => ReactElement }).type
    const tree = inner({ gadget: LAMP, ownerName: 'Ada', onSelectOwner: (id) => picked.push(id) })
    const link = elements(tree).find((el) => el.type === AgentLink) as ReactElement<{ id: string; onSelect: (id: string) => void }>
    expect(link.props.id).toBe('ag_ada')
    const linkInner = (AgentLink as unknown as { type: (p: { id: string; name: string; onSelect?: (id: string) => void }) => ReactElement }).type
    const span = linkInner({ id: link.props.id, name: 'Ada', onSelect: link.props.onSelect }) as ReactElement<{ onClick: (e: MouseEvent) => void }>
    span.props.onClick({ preventDefault: () => {}, stopPropagation: () => {} } as unknown as MouseEvent)
    expect(picked).toEqual(['ag_ada'])
  })

  it('failure rate is failures over all uses, null without uses or without the field', () => {
    expect(gadgetFailureRate({ uses: 3, failures: 1 })).toBe(0.25)
    expect(gadgetFailureRate({ uses: 0, failures: 0 })).toBeNull()
    expect(gadgetFailureRate({ uses: 4 })).toBeNull()
  })
})

describe('gadget panel (connected)', () => {
  it('opens on the selected gadget with the roster name and closes on a null selection', () => {
    const st = useStore.getState()
    st.applyRoster(ROSTER)
    st.applyGadgets({ type: 'gadgets', rev: 1, items: [LAMP, OLD] })
    expect(ssr(<GadgetPanel />)).toBe('')
    selectGadget('gd_lamp')
    const html = ssr(<GadgetPanel />)
    expect(html).toContain('data-gadget-id="gd_lamp"')
    expect(html).toContain('data-agent-id="ag_ada">Ada</span>')
    expect(html).toContain('title="clear gadget selection (Esc)"')
    selectGadget(null)
    expect(ssr(<GadgetPanel />)).toBe('')
  })

  it('says so when the selected gadget left the registry, and drops a selection made in another run', () => {
    useStore.getState().applyGadgets({ type: 'gadgets', rev: 1, items: [LAMP] })
    selectGadget('gd_gone')
    expect(ssr(<GadgetPanel />)).toContain('Gadget gd_gone is not in the registry the server last sent.')
    selectGadget('gd_lamp')
    useStore.setState({ runId: 'another-run' })
    expect(ssr(<GadgetPanel />)).toBe('')
  })
})

describe('proposal log', () => {
  it('keeps gate outcomes the ring has evicted and reads only newer events on each sync', () => {
    const ring = new EventRing(4)
    const log = new ProposalLog()
    ring.push(gateEvent('gadget_verified', 4, { gadget_id: 'g1', name: 'beacon', owner: 'a1', stage: 'verified', reason: '', verified_without_tests: true }))
    ring.push(gateEvent('gadget_rejected', 5, { gadget_id: 'g2', name: 'horn', owner: 'a2', stage: 'tests', reason: 'assert failed' }))
    const first = log.sync(ring, 'r')
    expect(first.map((r) => [r.gadgetId, r.verified, r.stage, r.reason, r.withoutTests])).toEqual([
      ['g1', true, 'verified', null, true],
      ['g2', false, 'tests', 'assert failed', false],
    ])
    // an unchanged ring returns the same rows
    expect(log.sync(ring, 'r')).toBe(first)
    for (let i = 0; i < 5; i++) ring.push(otherEvent(6 + i))
    ring.push(gateEvent('gadget_rejected', 12, { gadget_id: 'g3', name: 'lamp', owner: 'a1', stage: 'static', reason: 'import os' }))
    expect(ring.atAbs(0)).toBeNull()
    expect(log.sync(ring, 'r').map((r) => r.gadgetId)).toEqual(['g1', 'g2', 'g3'])
    expect(proposalSummary(log.rows)).toBe('1 verified · 2 rejected (static 1, tests 1)')
    // another run starts over from what its ring holds
    expect(log.sync(new EventRing(4), 'r2')).toEqual([])
  })

  it('caps the rows it keeps', () => {
    const ring = new EventRing(MAX_PROPOSALS * 2)
    const log = new ProposalLog()
    for (let i = 0; i < MAX_PROPOSALS + 5; i++) ring.push(gateEvent('gadget_rejected', i, { gadget_id: `g${i}`, name: 'x', owner: 'a', stage: 'size', reason: 'big' }))
    const rows = log.sync(ring, 'r')
    expect(rows).toHaveLength(MAX_PROPOSALS)
    expect(rows[rows.length - 1]!.gadgetId).toBe(`g${MAX_PROPOSALS + 4}`)
  })
})

describe('gadgets card', () => {
  it('is an empty state before anything arrives', () => {
    const html = ssr(<GadgetsCard />)
    expect(html).toContain('<h2 class="card-title">Gadgets</h2>')
    expect(html).toContain(EMPTY_TEXT)
  })

  it('lists the registry, the proposals with stage and reason, and plots the daily use-failure rate', () => {
    const st = useStore.getState()
    st.applyRoster(ROSTER)
    st.applyGadgets({ type: 'gadgets', rev: 2, items: [LAMP] })
    st.applyEvent(gateEvent('gadget_verified', 40, { gadget_id: 'gd_lamp', name: 'beacon-ada', owner: 'ag_ada', stage: 'verified', reason: '', verified_without_tests: true }))
    st.applyEvent(gateEvent('gadget_rejected', 41, { gadget_id: 'gd_bad', name: 'horn-bo', owner: 'ag_bo', stage: 'tests', reason: 'AssertionError: value out of range' }))
    const day = (d: number, rate: number | null): DayRow => ({ day: d, kind: 'day', population: 6, gadget_use_failure_rate: rate }) as DayRow
    st.applyDay(day(2, 0.4))
    st.applyDay(day(1, null))
    selectGadget('gd_lamp')
    const html = ssr(<GadgetsCard />)
    expect(html).not.toContain(EMPTY_TEXT)
    // the inspector opens at the top of the card and the meta names the selected gadget
    expect(html.indexOf('class="gadget-inspector"')).toBeGreaterThan(-1)
    expect(html.indexOf('class="gadget-inspector"')).toBeLessThan(html.indexOf('class="gadget-registry'))
    expect(html).toContain('<div class="card-meta"><span class="muted small">beacon-ada</span></div>')
    // registry
    expect(html).toMatch(/<li data-gadget-id="gd_lamp"[^>]*>.*beacon-ada.*Ada.*forage_bonus.*3 used · 2 failed \(40%\)/)
    // proposals, newest first, with stage and reason
    const list = html.slice(html.indexOf('class="gadget-proposals'))
    const rows = list.split('<li').slice(1)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('data-stage="tests"')
    expect(rows[0]).toContain('<span class="tag rejected">tests</span>')
    expect(rows[0]).toContain('AssertionError: value out of range')
    expect(rows[1]).toContain('data-stage="verified"')
    expect(rows[1]).toContain('<span class="tag approved">verified</span>')
    expect(rows[1]).toContain('untested')
    expect(html).toContain('1 verified · 1 rejected (tests 1)')
    // daily rate chart from the day rows
    expect(html).toContain('aria-label="gadget use-failure rate per day"')
    expect(html).toContain('>40%</text>')
  })

  it('orders the day rows and turns a missing rate into a gap', () => {
    const rows = [{ day: 3, gadget_use_failure_rate: 0.5 }, { day: 1, gadget_use_failure_rate: Number.NaN }, { day: 2 }] as DayRow[]
    const { days, series } = dailyFailureRate(rows)
    expect(days).toEqual([1, 2, 3])
    expect(series[0]!.values).toEqual([null, null, 0.5])
  })
})

describe('live use counts', () => {
  const pushed = (rev: number, uses: number, failures: number): GadgetsMsg => ({ type: 'gadgets', rev, items: [{ ...LAMP, uses, failures }] })
  /** A stub GET /api/gadgets answering with whatever `server` holds, counting the requests. */
  function stubServer(initial: GadgetsMsg) {
    const server = { msg: initial, calls: 0, fail: false }
    const fetcher = async (path: string): Promise<unknown> => {
      expect(path).toBe('/api/gadgets')
      server.calls++
      if (server.fail) throw new Error('server gone')
      return structuredClone(server.msg)
    }
    return { server, fetcher }
  }

  it('picks the fetched registry over the push it was made on, and a newer push over an older fetch', () => {
    const p1 = pushed(1, 1, 0)
    const f = { msg: pushed(1, 3, 1), base: p1, tick: 50, runId: 'r' }
    expect(freshestRegistry(p1, f, 'r')).toEqual({ msg: f.msg, asOfTick: 50 })
    // a fetch that already saw a newer rev is newer than the push
    expect(freshestRegistry(p1, { ...f, msg: pushed(2, 3, 1), base: null }, 'r').asOfTick).toBe(50)
    // a later push (a new gadget was verified) supersedes the fetch
    const p2 = pushed(2, 3, 1)
    expect(freshestRegistry(p2, f, 'r')).toEqual({ msg: p2, asOfTick: null })
    // a fetch from another run never shows
    expect(freshestRegistry(p1, f, 'other')).toEqual({ msg: p1, asOfTick: null })
    expect(freshestRegistry(p1, null, 'r')).toEqual({ msg: p1, asOfTick: null })
  })

  it('serves uses recorded after the last verification, which the rev-gated push never delivers', async () => {
    useStore.setState({ runId: 'r' })
    const st = useStore.getState()
    st.applyRoster(ROSTER)
    st.applyGadgets(pushed(1, 1, 0))
    atTick(30)
    // the server records more uses but verifies no new gadget: same rev, and the store keeps the first push
    st.applyGadgets(pushed(1, 3, 1))
    expect(useStore.getState().gadgets!.items[0]!.uses).toBe(1)
    selectGadget('gd_lamp')
    let html = ssr(<GadgetPanel />)
    expect(html).toMatch(/data-field="uses" class="tnum">1</)
    expect(html).toContain(`data-field="counts-as-of" class="muted" title="${COUNTS_STALE_TITLE}">as of the last registry update<`)

    const { server, fetcher } = stubServer(pushed(1, 3, 1))
    expect(await refreshGadgetCounts(fetcher)).toBe(true)
    html = ssr(<GadgetPanel />)
    expect(html).toMatch(/data-field="uses" class="tnum">3</)
    expect(html).toMatch(/data-field="failures" class="tnum">1<span class="muted"> · 25% of uses failed<\/span>/)
    expect(html).toContain('data-field="counts-as-of" class="muted">as of tick 30<')
    const card = ssr(<GadgetsCard />)
    expect(card).toContain('<div class="chart-sub" data-field="counts-as-of">uses and failures as of tick 30</div>')
    expect(card).toMatch(/<li data-gadget-id="gd_lamp"[^>]*>.*3 used · 1 failed \(25%\)/)
    expect(server.calls).toBe(1)
  })

  it('asks again only when the run has moved on, one request at a time, and keeps the push on a failure', async () => {
    useStore.setState({ runId: 'r' })
    const { server, fetcher } = stubServer(pushed(1, 1, 0))
    // nothing pushed yet in this run: nothing to refresh
    expect(await refreshGadgetCounts(fetcher)).toBe(false)
    expect(server.calls).toBe(0)
    useStore.getState().applyGadgets(pushed(1, 1, 0))
    atTick(10)
    const [a, b] = [refreshGadgetCounts(fetcher), refreshGadgetCounts(fetcher)]
    expect(await a).toBe(true)
    expect(await b).toBe(true)
    expect(server.calls).toBe(1)
    // same tick: the counts cannot have changed
    expect(await refreshGadgetCounts(fetcher)).toBe(false)
    expect(server.calls).toBe(1)
    atTick(11)
    server.msg = pushed(1, 2, 0)
    expect(await refreshGadgetCounts(fetcher)).toBe(true)
    expect(useGadgetCounts.getState().fetched!.msg.items[0]!.uses).toBe(2)
    // a failed request is reported and the last good fetch stays
    atTick(12)
    server.fail = true
    expect(await refreshGadgetCounts(fetcher)).toBe(false)
    expect(useGadgetCounts.getState()).toMatchObject({ failed: true, fetched: { tick: 11 } })
    expect(ssr(<GadgetsCard />)).toContain('data-field="counts-as-of">uses and failures as of tick 11; the last refresh failed</div>')
    // so is an answer that is not a registry
    server.fail = false
    expect(await refreshGadgetCounts(async () => ({ detail: 'nope' }))).toBe(false)
    expect(useGadgetCounts.getState().failed).toBe(true)
    // a run that changes while the request is out discards the answer
    atTick(13)
    let release: (v: unknown) => void = () => {}
    const pending = refreshGadgetCounts(() => new Promise((res) => (release = res)))
    useStore.setState({ runId: 'r2' })
    release(pushed(1, 9, 9))
    expect(await pending).toBe(false)
    expect(useGadgetCounts.getState().fetched!.runId).toBe('r')
  })

  it('the refresher in the card body adds no markup', () => {
    useStore.setState({ runId: 'r' })
    useStore.getState().applyGadgets(pushed(1, 1, 0))
    expect(ssr(<RegistryRefresh />)).toBe('')
  })

  it('polls on an interval while the run advances, and never in fixture mode', async () => {
    vi.useFakeTimers()
    useStore.setState({ runId: 'r' })
    useStore.getState().applyGadgets(pushed(1, 1, 0))
    atTick(5)
    const { server, fetcher } = stubServer(pushed(1, 1, 0))

    const none = startRegistryRefresh({ fixture: true, fetcher })
    await vi.advanceTimersByTimeAsync(REFRESH_MS * 3)
    none()
    expect(server.calls).toBe(0)

    const stop = startRegistryRefresh({ fixture: false, fetcher })
    await vi.advanceTimersByTimeAsync(0)
    expect(server.calls).toBe(1)
    // the tick did not move: the interval does not ask
    await vi.advanceTimersByTimeAsync(REFRESH_MS * 2)
    expect(server.calls).toBe(1)
    atTick(6)
    server.msg = pushed(1, 4, 2)
    await vi.advanceTimersByTimeAsync(REFRESH_MS)
    expect(server.calls).toBe(2)
    expect(useGadgetCounts.getState().fetched).toMatchObject({ tick: 6, msg: { items: [{ uses: 4, failures: 2 }] } })
    stop()
    atTick(7)
    await vi.advanceTimersByTimeAsync(REFRESH_MS * 2)
    expect(server.calls).toBe(2)
  })
})
