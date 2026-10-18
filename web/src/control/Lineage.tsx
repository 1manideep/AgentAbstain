import { memo, useEffect, useMemo, useState, type CSSProperties } from 'react'
import type { GraveyardResponse, MemorySummaryItem, PolicyResponse, PolicySource, PolicyVersion, TreeNode, TreeResponse } from '../protocol'
import { apiGet, FIXTURE_MODE } from '../net/api'
import { treeFromRoster } from '../state/lineage'
import { selectTierColor, useStore } from '../state/store'
import { pollMemorySummary } from './cards/MemoryCard'
import { INK, SERIES } from './charts/chartTheme'

/**
 * The lineage panel (PLAN §4.7 "a browsable family tree"; MEMORY_EVOLUTION §5.10 "a practice lineage tree (the lineage
 * panel, coloured by source)"). Dots wear the tier colour, or with "colour by policy source" the source of each agent's
 * current policy version (GET /api/memory/summary). A ring marks a policy copied from the parent at birth and edited by
 * the agent since. Archived rows show the cause of death from GET /api/graveyard. Text only, never HTML.
 */

export type LineageColour = 'tier' | 'source'

/** The six policy_versions sources (void/db.py CHECK), in legend order. */
// oxlint-disable-next-line react/only-export-components
export const POLICY_SOURCES: readonly PolicySource[] = ['seed', 'inherited', 'self_edit', 'adapted', 'mutated', 'adopted']

/** Colour per policy source: seed is neutral, every evolved source wears its own series slot. */
// oxlint-disable-next-line react/only-export-components
export const POLICY_SOURCE_COLORS: Readonly<Record<PolicySource, string>> = {
  seed: INK.muted,
  inherited: SERIES[6],
  self_edit: SERIES[0],
  adapted: SERIES[2],
  mutated: SERIES[1],
  adopted: SERIES[4],
}

/** An agent without a policy (the policy layer is off, or the summary does not list it) is drawn hollow. */
export const NO_POLICY_COLOR = 'transparent'
/** A source the client does not know (a newer server). */
export const OTHER_SOURCE_COLOR = INK.secondary
/** The ring around an inherited-then-edited policy's dot. */
export const INHERITED_EDITED_RING = POLICY_SOURCE_COLORS.inherited

// oxlint-disable-next-line react/only-export-components
export const SOURCE_LABELS: Readonly<Record<PolicySource, string>> = {
  seed: 'seed',
  inherited: 'inherited',
  self_edit: 'self edit',
  adapted: 'adapted',
  mutated: 'mutated',
  adopted: 'adopted',
}

const isSource = (s: string): s is PolicySource => Object.hasOwn(POLICY_SOURCE_COLORS, s)

/** The dot colour for a policy source; null (no policy) is hollow. */
// oxlint-disable-next-line react/only-export-components
export function policySourceColor(source: string | null | undefined): string {
  if (source === null || source === undefined || source === '') return NO_POLICY_COLOR
  return isSource(source) ? POLICY_SOURCE_COLORS[source] : OTHER_SOURCE_COLOR
}

/** Versions the kernel copies from the parent at birth (§5.7: inherit verbatim, kernel mutation). */
const BIRTH_COPY: ReadonlySet<string> = new Set(['inherited', 'mutated'])

/**
 * Whether an agent's current policy came from its parent and has since been edited by the agent itself:
 * `adapted` always (the child's own edit of the inherited text, §5.7); `self_edit` or `adopted` when the agent has a
 * parent and its version history holds a version copied at birth (`inherited`, `mutated`); otherwise false. Returns
 * null when the answer needs a history that is not loaded. Founders and arrivals (generation 0) start from a seed.
 */
// oxlint-disable-next-line react/only-export-components
export function inheritedEdited(source: string | null | undefined, generation: number, history: ReadonlyArray<Pick<PolicyVersion, 'source'>> | null | undefined): boolean | null {
  if (source === 'adapted') return true
  if (source !== 'self_edit' && source !== 'adopted') return false
  if (!(generation > 0)) return false
  if (!history) return null
  return history.some((v) => BIRTH_COPY.has(v.source))
}

/** True when {@link inheritedEdited} needs the agent's policy history to answer. */
// oxlint-disable-next-line react/only-export-components
export function needsPolicyHistory(source: string | null | undefined, generation: number): boolean {
  return inheritedEdited(source, generation, null) === null
}

const CAUSE_TITLES: Readonly<Record<string, string>> = {
  gate: 'could not afford its next call (the reservation gate refused it)',
  overrun: 'a call cost more than the hold reserved for it',
}

/** Hover text for a cause of death (void/agents/lifecycle.py, void/sim/loop.py); unknown causes explain themselves. */
// oxlint-disable-next-line react/only-export-components
export function causeTitle(cause: string | null | undefined): string {
  if (!cause) return 'cause not recorded'
  return CAUSE_TITLES[cause] ?? cause
}

/** Graveyard rows by agent id: the cause of death, null when the death event had none. */
// oxlint-disable-next-line react/only-export-components
export function causesFrom(r: GraveyardResponse | null | undefined): Map<string, string | null> {
  const out = new Map<string, string | null>()
  for (const a of Array.isArray(r?.agents) ? r.agents : []) {
    if (a && typeof a.id === 'string') out.set(a.id, typeof a.cause === 'string' && a.cause ? a.cause : null)
  }
  return out
}

/** What the source colouring knows about one agent's current policy. */
export interface PolicyInfo {
  source: string | null
  version: number | null
  /** See {@link inheritedEdited}; null while the history it needs is loading. */
  inheritedEdited: boolean | null
}

const historyKey = (id: string, version: number | null) => `${id}:${version ?? '-'}`

function generations(roots: readonly TreeNode[]): Map<string, number> {
  const out = new Map<string, number>()
  const walk = (list: readonly TreeNode[]) => {
    for (const n of list) {
      out.set(n.id, n.generation)
      walk(n.children)
    }
  }
  walk(roots)
  return out
}

/** Per agent of the summary: its source, version and inherited-then-edited mark from the loaded histories. */
// oxlint-disable-next-line react/only-export-components
export function policyInfo(
  summary: readonly MemorySummaryItem[] | null,
  roots: readonly TreeNode[],
  marks: ReadonlyMap<string, boolean>,
): Map<string, PolicyInfo> {
  const gen = generations(roots)
  const out = new Map<string, PolicyInfo>()
  for (const s of summary ?? []) {
    if (!s || typeof s.agent_id !== 'string') continue
    const source = typeof s.policy_source === 'string' && s.policy_source ? s.policy_source : null
    const version = typeof s.policy_version === 'number' ? s.policy_version : null
    const g = gen.get(s.agent_id) ?? 0
    let mark = inheritedEdited(source, g, null)
    if (mark === null) mark = marks.get(historyKey(s.agent_id, version)) ?? null
    out.set(s.agent_id, { source, version, inheritedEdited: mark })
  }
  return out
}

// ------------------------------------------------------------------ view

const HOLLOW: CSSProperties = { background: 'transparent', boxShadow: `inset 0 0 0 1.5px ${INK.muted}` }
const RING = `0 0 0 1.5px ${INK.surface}, 0 0 0 3px ${INHERITED_EDITED_RING}`

function dotStyle(colour: LineageColour, n: TreeNode, tierColor: (tier: string) => string, p: PolicyInfo | undefined): CSSProperties {
  if (colour === 'tier') return { background: tierColor(n.tier) }
  const c = policySourceColor(p?.source)
  const base = c === NO_POLICY_COLOR ? HOLLOW : { background: c }
  return p?.inheritedEdited ? { ...base, boxShadow: base.boxShadow ? `${base.boxShadow}, ${RING}` : RING } : base
}

function dotTitle(colour: LineageColour, n: TreeNode, p: PolicyInfo | undefined): string {
  if (colour === 'tier') return `tier ${n.tier}`
  if (!p) return 'policy not known'
  if (p.source === null) return 'no policy'
  const v = p.version !== null ? ` v${p.version}` : ''
  const mark = p.inheritedEdited ? ', copied from the parent at birth and edited since' : p.inheritedEdited === null ? ', history loading' : ''
  return `policy source ${p.source}${v}${mark}`
}

interface RowProps {
  n: TreeNode
  depth: number
  colour: LineageColour
  tierColor: (tier: string) => string
  policy: ReadonlyMap<string, PolicyInfo>
  causes: ReadonlyMap<string, string | null>
  selectedId: string | null
  onSelect: (id: string) => void
}

const Node = memo(function Node({ n, depth, colour, tierColor, policy, causes, selectedId, onSelect }: RowProps) {
  const dead = n.status !== 'alive'
  const p = policy.get(n.id)
  const known = causes.has(n.id)
  const cause = causes.get(n.id) ?? null
  let meta = `g${n.generation} · ${n.tier}`
  if (colour === 'source') {
    const src = p ? (p.source === null ? 'no policy' : p.source + (p.version !== null ? ` v${p.version}` : '')) : 'policy ?'
    meta = `g${n.generation} · ${src}`
  }
  return (
    <>
      <div
        className={'tree-row' + (n.id === selectedId ? ' selected' : '') + (dead ? ' dead' : '')}
        style={{ paddingLeft: 8 + depth * 16 }}
        onClick={() => onSelect(n.id)}
        data-agent-id={n.id}
        data-source={colour === 'source' ? (p?.source ?? 'none') : undefined}
      >
        <span className="tree-branch" aria-hidden="true">
          {depth > 0 ? '└' : ''}
        </span>
        <span className="tree-dot" style={dotStyle(colour, n, tierColor, p)} title={dotTitle(colour, n, p)} />
        <span className="tree-name">{n.name}</span>
        <span className="muted small">{meta}</span>
        {colour === 'source' && p?.inheritedEdited ? (
          <span className="muted small" data-mark="inherited-edited" title="copied from the parent at birth, edited by the agent since">
            inherited, edited
          </span>
        ) : null}
        <span className={'tree-state ' + (dead ? 'dead' : 'alive')}>
          {dead ? `✕ ${n.died_tick ?? ''}` : '● alive'}
          {dead && known ? (
            <span className="muted" data-cause={cause ?? ''} title={causeTitle(cause)}>
              {` · ${cause ?? 'cause unknown'}`}
            </span>
          ) : null}
        </span>
      </div>
      {n.children.map((c) => (
        <Node key={c.id} n={c} depth={depth + 1} colour={colour} tierColor={tierColor} policy={policy} causes={causes} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </>
  )
})

/** The legend of the source colouring: the six sources, no policy, and the inherited-then-edited ring. */
export const SourceLegend = memo(function SourceLegend() {
  return (
    <div className="chart-legend" aria-label="policy source colours">
      {POLICY_SOURCES.map((s) => (
        <span key={s} className="legend-item" data-source={s}>
          <span className="legend-swatch" style={{ background: POLICY_SOURCE_COLORS[s], borderRadius: '50%' }} />
          {SOURCE_LABELS[s]}
        </span>
      ))}
      <span className="legend-item">
        <span className="legend-swatch" style={{ ...HOLLOW, borderRadius: '50%' }} />
        no policy
      </span>
      <span className="legend-item">
        <span className="legend-swatch" style={{ background: INK.muted, borderRadius: '50%', boxShadow: RING }} />
        inherited, then edited
      </span>
    </div>
  )
})

export type PolicyStatus = 'idle' | 'ok' | 'unavailable' | 'fixture'

/** One line under the legend saying what the colouring rests on; null when there is nothing to say. */
// oxlint-disable-next-line react/only-export-components
export function policyNote(status: PolicyStatus, policy: ReadonlyMap<string, PolicyInfo>, policyLayer: boolean | undefined): string | null {
  if (status === 'fixture') return "Policy sources come from the recording's memory summary; this fixture playback has none."
  if (status === 'unavailable') return 'Policy sources unavailable: /api/memory/summary did not answer.'
  if (status === 'idle') return 'Loading policy sources...'
  let any = false
  let pending = 0
  for (const p of policy.values()) {
    if (p.source !== null) any = true
    if (p.inheritedEdited === null) pending++
  }
  if (!any) return policyLayer === false ? 'This run has no policy file: every dot is hollow.' : 'No agent has a policy version yet.'
  return pending > 0 ? `Loading ${pending} policy ${pending === 1 ? 'history' : 'histories'} for the inherited-then-edited mark...` : null
}

export interface LineageTreeProps {
  roots: readonly TreeNode[]
  colour: LineageColour
  tierColor: (tier: string) => string
  policy?: ReadonlyMap<string, PolicyInfo>
  causes?: ReadonlyMap<string, string | null>
  selectedId: string | null
  onSelect: (id: string) => void
}

const NO_POLICY: ReadonlyMap<string, PolicyInfo> = new Map()
const NO_CAUSES: ReadonlyMap<string, string | null> = new Map()

/** The tree from props (the panel itself reads the store and the endpoints). */
export const LineageTree = memo(function LineageTree({ roots, colour, tierColor, policy = NO_POLICY, causes = NO_CAUSES, selectedId, onSelect }: LineageTreeProps) {
  if (roots.length === 0) return <div className="muted small">No agents yet.</div>
  return (
    <div className="tree">
      {roots.map((n) => (
        <Node key={n.id} n={n} depth={0} colour={colour} tierColor={tierColor} policy={policy} causes={causes} selectedId={selectedId} onSelect={onSelect} />
      ))}
    </div>
  )
})

const ColourToggle = memo(function ColourToggle({ colour, onChange }: { colour: LineageColour; onChange: (c: LineageColour) => void }) {
  const chip = (c: LineageColour, label: string) => (
    <button type="button" className={'chip' + (colour === c ? ' active' : '')} aria-pressed={colour === c} onClick={() => onChange(c)}>
      {label}
    </button>
  )
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '6px 0 2px' }}>
      <span className="muted small">colour by</span>
      {chip('tier', 'tier')}
      {chip('source', 'policy source')}
    </div>
  )
})

// ------------------------------------------------------------------ data

const COLOUR_KEY = 'void.lineage.colour'
const DEBOUNCE_MS = 800
/** Policy histories fetched per round, a few at a time, so a large population does not burst the server. */
const HISTORY_PER_ROUND = 24
const HISTORY_CONCURRENCY = 3

function readColour(): LineageColour {
  try {
    return localStorage.getItem(COLOUR_KEY) === 'source' ? 'source' : 'tier'
  } catch {
    return 'tier'
  }
}

function writeColour(c: LineageColour): void {
  try {
    localStorage.setItem(COLOUR_KEY, c)
  } catch {
    /* private mode: the choice lasts this page only */
  }
}

interface SummaryState {
  runId: string | null
  items: MemorySummaryItem[] | null
  /** 'fixture': fixture playback whose recording did not answer the summary. */
  status: 'idle' | 'ok' | 'unavailable' | 'fixture'
}

interface MarksState {
  runId: string | null
  marks: Map<string, boolean>
}

export interface LineageProps {
  /**
   * No route behind the panel at all: the roster's tree, and the source colouring says fixture playback has none,
   * without asking. Fixture playback does not need it: the api layer answers the graveyard, the memory summary and the
   * policy histories from the recording, and a summary the recording lacks shows the same note.
   */
  fixture?: boolean
}

/**
 * The panel: the tree (GET /api/tree, or the roster's), causes of death (GET /api/graveyard) and, coloured by source,
 * the memory summary and the policy histories the inherited-then-edited mark needs. Fixture playback asks the api layer
 * for all of them but the tree: the recorded tree is the end of the run, ahead of the playback, so the roster's is kept.
 */
export const Lineage = memo(function Lineage({ fixture = false }: LineageProps) {
  const roster = useStore((s) => s.roster)
  const rosterRev = useStore((s) => s.rosterRev)
  const config = useStore((s) => s.config)
  const runId = useStore((s) => s.runId)
  const selectedId = useStore((s) => s.selectedAgentId)
  const select = useStore((s) => s.select)
  const [remote, setRemote] = useState<TreeNode[] | null>(null)
  const [causes, setCauses] = useState<Map<string, string | null>>(() => new Map())
  const [colour, setColour] = useState<LineageColour>(readColour)
  const [summary, setSummary] = useState<SummaryState>({ runId: null, items: null, status: 'idle' })
  const [marks, setMarks] = useState<MarksState>({ runId: null, marks: new Map() })

  // the tree and the graveyard change with the roster; both are asked a moment after it settles
  useEffect(() => {
    if (fixture) return
    let cancelled = false
    const t = setTimeout(() => {
      if (!FIXTURE_MODE) {
        apiGet<TreeResponse>('/api/tree')
          .then((r) => {
            if (!cancelled && Array.isArray(r.roots)) setRemote(r.roots)
          })
          .catch(() => {
            if (!cancelled) setRemote(null)
          })
      }
      // a recorded death's cause never changes, so the recording's graveyard holds for every dead agent of the playback
      apiGet<GraveyardResponse>('/api/graveyard')
        .then((r) => {
          if (!cancelled) setCauses(causesFrom(r))
        })
        .catch(() => {
          /* keep the causes already known: a dead agent stays dead */
        })
    }, DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [rosterRev, fixture])

  // policy sources move nightly without a roster change, so the summary is polled while the colouring shows them
  useEffect(() => {
    if (colour !== 'source' || fixture || runId === null) return
    return pollMemorySummary(
      (items) => setSummary({ runId, items, status: 'ok' }),
      () => setSummary((prev) => (prev.runId === runId && prev.items ? prev : { runId, items: null, status: FIXTURE_MODE ? 'fixture' : 'unavailable' })),
    )
  }, [colour, fixture, runId])

  const roots = useMemo(() => remote ?? treeFromRoster(roster), [remote, roster])
  const items = summary.runId === runId ? summary.items : null
  const known = marks.runId === runId ? marks.marks : null
  const policy = useMemo(() => policyInfo(items, roots, known ?? new Map()), [items, roots, known])

  // agents whose mark needs their history: self_edit or adopted with a parent; fetched once per policy version
  const wanted = useMemo(() => {
    const out: Array<{ id: string; key: string }> = []
    for (const [id, p] of policy) if (p.inheritedEdited === null) out.push({ id, key: historyKey(id, p.version) })
    return out.slice(0, HISTORY_PER_ROUND)
  }, [policy])
  // a string, so a summary poll that names the same agents and versions does not restart the fetches
  const wantedJson = JSON.stringify(wanted)

  useEffect(() => {
    const list = JSON.parse(wantedJson) as Array<{ id: string; key: string }>
    if (colour !== 'source' || fixture || runId === null || list.length === 0) return
    let cancelled = false
    const run = async () => {
      const found = new Map<string, boolean>()
      for (let i = 0; i < list.length; i += HISTORY_CONCURRENCY) {
        const batch = list.slice(i, i + HISTORY_CONCURRENCY)
        const got = await Promise.all(
          batch.map((w) =>
            apiGet<PolicyResponse>(`/api/agents/${encodeURIComponent(w.id)}/policy`)
              .then((r) => [w.key, (Array.isArray(r?.versions) ? r.versions : []).some((v) => BIRTH_COPY.has(v.source))] as const)
              // a failed history stays unknown and is asked again when the summary changes the version
              .catch(() => null),
          ),
        )
        if (cancelled) return
        for (const g of got) if (g) found.set(g[0], g[1])
      }
      if (cancelled || found.size === 0) return
      setMarks((prev) => {
        const base = prev.runId === runId ? prev.marks : new Map<string, boolean>()
        const next = new Map(base)
        for (const [k, v] of found) next.set(k, v)
        return { runId, marks: next }
      })
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [colour, fixture, runId, wantedJson])

  const tierColor = useMemo(() => (tier: string) => selectTierColor(config, tier), [config])
  const onColour = (c: LineageColour) => {
    setColour(c)
    writeColour(c)
  }
  const status: PolicyStatus = fixture ? 'fixture' : summary.runId === runId ? summary.status : 'idle'
  const note = colour === 'source' ? policyNote(status, policy, config?.memory_layer?.policy) : null
  return (
    <>
      <ColourToggle colour={colour} onChange={onColour} />
      {colour === 'source' ? <SourceLegend /> : null}
      {note ? <div className="chart-sub">{note}</div> : null}
      <LineageTree roots={roots} colour={colour} tierColor={tierColor} policy={policy} causes={causes} selectedId={selectedId} onSelect={select} />
    </>
  )
})
