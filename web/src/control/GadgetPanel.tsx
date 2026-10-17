import { memo, useCallback, useEffect, useRef } from 'react'
import { create } from 'zustand'
import type { GadgetItem } from '../protocol'
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
}

/**
 * One gadget from the registry the server last sent: owner, verified effect, purpose, use counts and whether the gate
 * verified it without running its tests (DESIGN §12 `gate_enabled: false`). Fields an older server does not send
 * read as such rather than as zero.
 */
export const GadgetDetails = memo(function GadgetDetails({ gadget: g, ownerName, onSelectOwner, onClose }: GadgetDetailsProps) {
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

/** The selected gadget's inspector, or nothing when no gadget is selected in this run. */
export const GadgetPanel = memo(function GadgetPanel() {
  const id = useSelectedGadgetId()
  const gadgets = useStore((s) => s.gadgets)
  const ownerName = useStore((s) => {
    const g = id ? s.gadgets?.items.find((it) => it.id === id) : undefined
    return g ? (s.rosterById.get(g.owner)?.name ?? g.owner) : ''
  })
  const selectAgent = useStore((s) => s.select)
  const clear = useCallback(() => selectGadget(null), [])
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (id && el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [id])
  if (!id) return null
  const gadget = gadgets?.items.find((g) => g.id === id)
  return (
    <div ref={ref} className="gadget-inspector">
      {gadget ? (
        <GadgetDetails gadget={gadget} ownerName={ownerName} onSelectOwner={selectAgent} onClose={clear} />
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
