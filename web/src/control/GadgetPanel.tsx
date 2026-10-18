import { memo, useCallback, useEffect, useMemo, useRef } from 'react'
import { create } from 'zustand'
import { apiGet, FIXTURE_MODE } from '../net/api'
import type { GadgetItem, GadgetsMsg } from '../protocol'
import { useStore } from '../state/store'
import { fmtNum, fmtPct, STATUS } from './charts/chartTheme'
import { AgentLink } from './PolicyHistory'

// ---------------------------------------------------------------- selection (a local slice: store.ts stays untouched)

export interface GadgetSelectionState {
  /** The gadget picked in the scene or the registry list, or null. */
  id: string | null
  /** The run the pick was made in: a selection never carries over into another run. */
  runId: string | null
  select: (id: string | null) => void
}

// oxlint-disable-next-line react/only-export-components
export const useGadgetSelection = create<GadgetSelectionState>()((set, get) => ({
  id: null,
  runId: null,
  select: (id) => {
    const runId = useStore.getState().runId
    const cur = get()
    if (cur.id !== id || cur.runId !== runId) set({ id, runId })
  },
}))

/** Select a gadget (or clear with null); callable outside React, e.g. from a scene pointer handler. */
// oxlint-disable-next-line react/only-export-components
export function selectGadget(id: string | null): void {
  useGadgetSelection.getState().select(id)
}

/** The selected gadget's id in the current run, or null. */
// oxlint-disable-next-line react/only-export-components
export function useSelectedGadgetId(): string | null {
  const id = useGadgetSelection((s) => s.id)
  const pickedIn = useGadgetSelection((s) => s.runId)
  const runId = useStore((s) => s.runId)
  return id !== null && pickedIn === runId ? id : null
}

// ---------------------------------------------------------------- live use counts (a second local slice)

/*
 * The server pushes the registry only when the number of verified gadgets changes (gadgets_rev in void/sim/loop.py),
 * and the store keeps a push only when its rev is newer. Uses and failures recorded after the last verification
 * therefore never arrive on the socket. While the gadgets card is open the registry is refetched from
 * GET /api/gadgets, which builds the same message from the registry at request time: on a gadget selection, and
 * every REFRESH_MS while the run's tick advances. Folding use_gadget / gadget_use_failed events onto the pushed copy
 * is not exact: the hub fans events out from an async consumer, so use events of tick T can reach the client after
 * the registry message built at the end of tick T, and that message carries no seq or tick to fold from.
 */

/** How often the open gadgets card refetches the registry while the run's tick advances. */
export const REFRESH_MS = 5000

/** A registry fetched from GET /api/gadgets. */
export interface FetchedRegistry {
  msg: GadgetsMsg
  /** The pushed registry the fetch was made over; a later push supersedes the fetch. */
  base: GadgetsMsg | null
  /** The newest snapshot tick the client had when it asked: the counts are at least this recent. */
  tick: number
  runId: string | null
}

export interface GadgetCountsState {
  fetched: FetchedRegistry | null
  /** The last refetch failed; the counts shown are the pushed ones. */
  failed: boolean
}

// oxlint-disable-next-line react/only-export-components
export const useGadgetCounts = create<GadgetCountsState>()(() => ({ fetched: null, failed: false }))

/** The registry the card shows and how current its uses and failures are. */
export interface RegistryView {
  msg: GadgetsMsg | null
  /** The tick the counts are current at, or null when they are as of the last registry push. */
  asOfTick: number | null
}

/**
 * The fresher of the pushed registry and the fetched one. A fetch counts when it belongs to this run and was made
 * over the current push (or saw a newer rev than the push); a later push, which carries a new gadget, wins.
 */
// oxlint-disable-next-line react/only-export-components
export function freshestRegistry(pushed: GadgetsMsg | null, fetched: FetchedRegistry | null, runId: string | null): RegistryView {
  if (fetched && fetched.runId === runId) {
    const newerRev = pushed === null || fetched.msg.rev > pushed.rev
    const overThisPush = pushed !== null && fetched.msg.rev === pushed.rev && fetched.base === pushed
    if (newerRev || overThisPush) return { msg: fetched.msg, asOfTick: fetched.tick }
  }
  return { msg: pushed, asOfTick: null }
}

/** The registry to show: the pushed copy, or a fresher fetched one. */
// oxlint-disable-next-line react/only-export-components
export function useRegistryView(): RegistryView {
  const pushed = useStore((s) => s.gadgets)
  const runId = useStore((s) => s.runId)
  const fetched = useGadgetCounts((s) => s.fetched)
  return useMemo(() => freshestRegistry(pushed, fetched, runId), [pushed, fetched, runId])
}

const isGadgetsMsg = (v: unknown): v is GadgetsMsg =>
  !!v && typeof v === 'object' && typeof (v as GadgetsMsg).rev === 'number' && Array.isArray((v as GadgetsMsg).items)

export type RegistryFetcher = (path: string) => Promise<unknown>

let inflight: Promise<boolean> | null = null

/**
 * Refetch the registry unless nothing can have changed: no registry pushed yet in this run, or no newer snapshot
 * tick since the last fetch made over the current push. One request at a time. Resolves true when it stored a fetch.
 */
// oxlint-disable-next-line react/only-export-components
export function refreshGadgetCounts(fetcher: RegistryFetcher = apiGet): Promise<boolean> {
  if (inflight) return inflight
  const s = useStore.getState()
  const { runId, gadgets: base } = s
  const tick = s.world.tick
  const cur = useGadgetCounts.getState().fetched
  if (base === null || (cur && cur.runId === runId && cur.base === base && cur.tick >= tick)) return Promise.resolve(false)
  const run = async (): Promise<boolean> => {
    try {
      const msg = await fetcher('/api/gadgets')
      if (!isGadgetsMsg(msg)) throw new Error('not a gadgets message')
      if (useStore.getState().runId !== runId) return false // the run changed while the request was out
      useGadgetCounts.setState({ fetched: { msg, base, tick, runId }, failed: false })
      return true
    } catch {
      useGadgetCounts.setState({ failed: true })
      return false
    } finally {
      inflight = null
    }
  }
  inflight = run()
  return inflight
}

export interface RefreshOptions {
  /** Fixture playback has no server to ask; nothing is fetched. */
  fixture?: boolean
  intervalMs?: number
  fetcher?: RegistryFetcher
}

/** Refetch now and every `intervalMs` (skipped while the page is hidden); returns the stop function. */
// oxlint-disable-next-line react/only-export-components
export function startRegistryRefresh({ fixture = FIXTURE_MODE, intervalMs = REFRESH_MS, fetcher = apiGet }: RefreshOptions = {}): () => void {
  if (fixture) return () => {}
  void refreshGadgetCounts(fetcher)
  const timer = setInterval(() => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    void refreshGadgetCounts(fetcher)
  }, intervalMs)
  return () => clearInterval(timer)
}

/** Keep the registry counts current while the calling card is mounted; a new selection refetches at once. */
// oxlint-disable-next-line react/only-export-components
export function useRegistryRefresh(options: RefreshOptions = {}): void {
  const { fixture = FIXTURE_MODE, intervalMs = REFRESH_MS, fetcher = apiGet } = options
  const selectedId = useSelectedGadgetId()
  const hasRegistry = useStore((s) => (s.gadgets?.items.length ?? 0) > 0)
  useEffect(() => {
    if (!hasRegistry) return
    return startRegistryRefresh({ fixture, intervalMs, fetcher })
  }, [fixture, intervalMs, fetcher, hasRegistry, selectedId])
}

/** Renders nothing; keeps the counts current while it is mounted (inside a card body: only while the card is open). */
export const RegistryRefresh = memo(function RegistryRefresh() {
  useRegistryRefresh()
  return null
})

/** "as of tick 120", or that the counts are as of the last registry push. */
// oxlint-disable-next-line react/only-export-components
export function countsAsOf(asOfTick: number | null): string {
  return asOfTick !== null ? `as of tick ${asOfTick}` : 'as of the last registry update'
}

/** Hover text for counts that are as of the last registry push. */
// oxlint-disable-next-line react/only-export-components
export const COUNTS_STALE_TITLE = 'the server pushes the registry when a gadget is verified; uses since then are not counted here'

// ---------------------------------------------------------------- formatting

const COLOR_RE = /^#[0-9a-f]{6}$/i
const FALLBACK_COLOR = '#8b93a7'

const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** failures / (uses + failures), or null when the gadget has no recorded use or the server sends no failures. */
// oxlint-disable-next-line react/only-export-components
export function gadgetFailureRate(g: Pick<GadgetItem, 'uses' | 'failures'>): number | null {
  const uses = fin(g.uses)
  const failures = fin(g.failures)
  if (uses === null || failures === null || uses + failures <= 0) return null
  return failures / (uses + failures)
}

function effectText(g: GadgetItem): string | null {
  const kind = typeof g.effect_kind === 'string' && g.effect_kind ? g.effect_kind : null
  const value = fin(g.effect_value)
  if (kind === null && value === null) return null
  return `${kind ?? 'unknown effect'}${value !== null ? ` ${fmtNum(value, 2)}` : ''}`
}

const NOT_SENT = <span className="muted">not sent by this server</span>

// ---------------------------------------------------------------- the inspector

interface GadgetDetailsProps {
  gadget: GadgetItem
  /** The owner's display name (the roster name, else the id). */
  ownerName: string
  /** Selects the owning agent; without it the owner is plain text. */
  onSelectOwner?: (id: string) => void
  onClose?: () => void
  /** The tick the uses and failures are current at (a registry fetch), or null: as of the last registry push. */
  countsAsOfTick?: number | null
}

/**
 * One gadget from the registry: owner, verified effect, purpose, use counts (with the tick they are current at) and
 * whether the gate verified it without running its tests (DESIGN §12 `gate_enabled: false`). Fields an older server
 * does not send read as such rather than as zero.
 */
export const GadgetDetails = memo(function GadgetDetails({ gadget: g, ownerName, onSelectOwner, onClose, countsAsOfTick = null }: GadgetDetailsProps) {
  const color = COLOR_RE.test(g.render?.color ?? '') ? g.render.color : FALLBACK_COLOR
  const effect = effectText(g)
  const rate = gadgetFailureRate(g)
  const failures = fin(g.failures)
  const purpose = typeof g.purpose === 'string' ? g.purpose.trim() : ''
  const flag = g.verified_without_tests
  const shape = typeof g.render?.shape === 'string' ? g.render.shape : '?'
  return (
    <div className="gadget-panel" data-gadget-id={g.id}>
      <div className="agent-head">
        <span className="tree-dot big" style={{ background: color }} />
        <span className="agent-name">{g.name}</span>
        <span className="muted">
          {shape}
          {g.render?.label ? ` · ${g.render.label}` : ''}
        </span>
        {flag === true ? (
          <span className="tag" data-flag="verified_without_tests" style={{ color: STATUS.warning }} title="the gate verified this gadget without running its tests">
            untested
          </span>
        ) : (
          <span className="tag approved">verified</span>
        )}
        <span className="spacer" />
        {onClose ? (
          <button type="button" className="btn tiny" onClick={onClose} title="clear gadget selection (Esc)">
            ✕
          </button>
        ) : null}
      </div>
      <dl className="kv">
        <dt>owner</dt>
        <dd>
          <AgentLink id={g.owner} name={ownerName} onSelect={onSelectOwner} />
        </dd>
        <dt>effect</dt>
        <dd data-field="effect">{effect ?? NOT_SENT}</dd>
        <dt>purpose</dt>
        <dd data-field="purpose" style={{ whiteSpace: 'normal' }}>
          {purpose ? purpose : g.purpose === undefined ? NOT_SENT : <span className="muted">none given</span>}
        </dd>
        <dt>uses</dt>
        <dd data-field="uses" className="tnum">
          {fin(g.uses) ?? '—'}
        </dd>
        <dt>failures</dt>
        <dd data-field="failures" className="tnum">
          {failures === null ? (
            NOT_SENT
          ) : (
            <>
              {failures}
              {rate !== null ? <span className="muted"> · {fmtPct(rate)} of uses failed</span> : null}
            </>
          )}
        </dd>
        <dt>counted</dt>
        <dd data-field="counts-as-of" className="muted" title={countsAsOfTick === null ? COUNTS_STALE_TITLE : undefined}>
          {countsAsOf(countsAsOfTick)}
        </dd>
        <dt>tests</dt>
        <dd data-field="verified_without_tests">
          {flag === undefined ? (
            NOT_SENT
          ) : flag ? (
            <span style={{ color: STATUS.warning }}>verified without tests (gate off)</span>
          ) : (
            <span className="ok-text">tests passed at the gate</span>
          )}
        </dd>
        <dt>created</dt>
        <dd className="tnum">{fin(g.created_tick) !== null ? `tick ${g.created_tick}` : '—'}</dd>
        <dt>position</dt>
        <dd className="tnum">
          {fmtNum(g.x, 1)}, {fmtNum(g.y, 1)}
        </dd>
      </dl>
    </div>
  )
})

/**
 * The selected gadget's inspector, or nothing when no gadget is selected in this run. It reads the fresher of the
 * pushed and the fetched registry; whoever renders it keeps the counts current with RegistryRefresh.
 */
export const GadgetPanel = memo(function GadgetPanel() {
  const id = useSelectedGadgetId()
  const view = useRegistryView()
  const gadget = id ? view.msg?.items.find((g) => g.id === id) : undefined
  const owner = gadget?.owner ?? null
  const ownerName = useStore((s) => (owner === null ? '' : (s.rosterById.get(owner)?.name ?? owner)))
  const selectAgent = useStore((s) => s.select)
  const clear = useCallback(() => selectGadget(null), [])
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (id && el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [id])
  if (!id) return null
  return (
    <div ref={ref} className="gadget-inspector">
      {gadget ? (
        <GadgetDetails gadget={gadget} ownerName={ownerName} onSelectOwner={selectAgent} onClose={clear} countsAsOfTick={view.asOfTick} />
      ) : (
        <div className="muted small">
          Gadget {id} is not in the registry the server last sent.{' '}
          <button type="button" className="btn tiny" onClick={clear}>
            clear
          </button>
        </div>
      )}
    </div>
  )
})
