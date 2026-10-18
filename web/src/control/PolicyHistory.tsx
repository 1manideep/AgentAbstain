import { memo, useEffect, useMemo, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import type { PolicyResponse, PolicyVersion } from '../protocol'
import { ApiError, apiGet, FIXTURE_MODE } from '../net/api'
import { fmtNum } from './charts/chartTheme'
import { POLICY_SOURCES } from './Lineage'
import { wordDiff } from './wordDiff'

const REFRESH_MS = 20000

/** The empty state of fixture playback whose recording has no policy history for the agent. */
export const FIXTURE_NO_POLICY = 'Policy history is not recorded in fixture playback.'

const KNOWN_SOURCES: ReadonlySet<string> = new Set(POLICY_SOURCES)

/** The source tag's classes (index.css .tag.src-*): a source the client does not know is src-other, never its raw text. */
const sourceClass = (source: string) => 'tag src-' + (KNOWN_SOURCES.has(source) ? source : 'other')

/**
 * Stands in for a line break while diffing. wordDiff splits on whitespace, so without it a policy's lines
 * (markdown bullets) would run together; the marker is a private-use character the cleaned text never holds.
 */
const NL = ''
const MARK_RE = /\r?\n/g
const UNMARK_RE = / ? ?/g

interface Piece {
  kind: 'same' | 'add' | 'del'
  text: string
}

/** Word diff of two policy texts that keeps line breaks; the pieces are plain strings for text nodes. */
function policyDiff(parent: string, child: string): Piece[] {
  const mark = (s: string) => s.replace(MARK_RE, ` ${NL} `)
  return wordDiff(mark(parent), mark(child)).map((t) => ({ kind: t.kind, text: t.text.replace(UNMARK_RE, '\n') }))
}

/** The diff as text runs: one span per changed run, a space between runs unless a line break already separates them. */
function renderDiff(pieces: Piece[]): ReactNode[] {
  const out: ReactNode[] = []
  pieces.forEach((p, k) => {
    const prev = pieces[k - 1]
    if (prev && !prev.text.endsWith('\n') && !p.text.startsWith('\n')) out.push(' ')
    out.push(
      <span key={k} className={'diff-' + p.kind}>
        {p.text}
      </span>,
    )
  })
  return out
}

/** A clickable agent name: a span with role=button (never a link), selecting the agent on click or Enter. */
export const AgentLink = memo(function AgentLink({
  id,
  name,
  onSelect,
}: {
  id: string
  name: string
  onSelect?: (id: string) => void
}) {
  if (!onSelect) return <span data-agent-id={id}>{name}</span>
  const go = (e: MouseEvent | KeyboardEvent) => {
    // inside a <summary> a click would also fold the version; selecting the agent is the only intent
    e.preventDefault()
    e.stopPropagation()
    onSelect(id)
  }
  return (
    <span
      className="wikilink"
      role="button"
      tabIndex={0}
      data-agent-id={id}
      onClick={go}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') go(e)
      }}
    >
      {name}
    </span>
  )
})

export interface PolicyHistoryListProps {
  versions: PolicyVersion[]
  /** Display name for an agent id (the roster's name); defaults to the id. */
  nameOf?: (id: string) => string
  /** Selects an agent (the store's `select`); without it source agents render as plain names. */
  onSelectAgent?: (id: string) => void
}

/**
 * Policy versions newest first. Each shows its source tag, tick and similarity to its parent, and a word diff
 * against the parent version (the same agent's previous version). A first version that was copied from another
 * agent (inherited, adopted) shows its full text, with the source agent as a link. Text only, never HTML.
 */
export const PolicyHistoryList = memo(function PolicyHistoryList({ versions, nameOf = (id) => id, onSelectAgent }: PolicyHistoryListProps) {
  const sorted = useMemo(() => [...versions].sort((a, b) => b.version - a.version), [versions])
  const byVersion = useMemo(() => new Map(versions.map((v) => [v.version, v])), [versions])
  if (sorted.length === 0) return <div className="muted small">No policy versions recorded.</div>
  return (
    <ol className="self-versions policy-versions">
      {sorted.map((v, i) => {
        const parent = v.parent_version != null ? byVersion.get(v.parent_version) : undefined
        const caption = parent
          ? `diff vs v${parent.version}`
          : v.parent_version != null
            ? `v${v.parent_version} not loaded`
            : v.source_agent_id
              ? 'copied'
              : 'first'
        return (
          <li key={v.version} className="self-version policy-version" data-version={v.version}>
            <details open={i === 0}>
              <summary className="self-head">
                <span className="tnum">v{v.version}</span>
                <span className={sourceClass(v.source)} data-source={v.source}>
                  {v.source}
                </span>
                <span className="muted small tnum">tick {v.tick}</span>
                {v.similarity_to_parent != null ? (
                  <span className="muted small tnum" title="word-set similarity to the parent text (1 = same words)">
                    sim {fmtNum(v.similarity_to_parent, 2)}
                  </span>
                ) : null}
                {v.source_agent_id ? (
                  <span className="muted small">
                    from <AgentLink id={v.source_agent_id} name={nameOf(v.source_agent_id)} onSelect={onSelectAgent} />
                  </span>
                ) : null}
                <span className="muted small">{caption}</span>
              </summary>
              <p className="self-text">{parent ? renderDiff(policyDiff(parent.text, v.text)) : v.text}</p>
            </details>
          </li>
        )
      })}
    </ol>
  )
})

/** What GET /api/agents/{id}/policy answered for one agent. */
export interface PolicyLoad {
  id: string
  /** null after a failure other than a 404 */
  versions: PolicyVersion[] | null
  /** The route answered 404: no policy yet, or (fixture playback) a recording without this agent's history. */
  missing: boolean
  err: string | null
}

/**
 * One agent's policy history through the api layer, which in fixture playback answers from the recording's api.json
 * (net/api.ts fixtureGet). Never rejects: a failure is in the result.
 */
// oxlint-disable-next-line react/only-export-components
export function fetchPolicyHistory(agentId: string, get: (path: string) => Promise<PolicyResponse> = apiGet): Promise<PolicyLoad> {
  return get(`/api/agents/${encodeURIComponent(agentId)}/policy`).then(
    (r): PolicyLoad => ({ id: agentId, versions: Array.isArray(r?.versions) ? r.versions : [], missing: false, err: null }),
    (e: unknown): PolicyLoad =>
      e instanceof ApiError && e.status === 404
        ? { id: agentId, versions: [], missing: true, err: null }
        : { id: agentId, versions: null, missing: false, err: e instanceof Error ? e.message : String(e) },
  )
}

export interface PolicyHistoryViewProps {
  /** The answer for the agent shown; null while it loads. */
  load: PolicyLoad | null
  /** Fixture playback: a 404 means the recording has no history for the agent. Defaults to the build's feed mode. */
  fixtureMode?: boolean
  nameOf?: (id: string) => string
  onSelectAgent?: (id: string) => void
}

/** The panel for one answer: loading, the error, the fixture's empty state for a 404 in fixture playback, or the list. */
export const PolicyHistoryView = memo(function PolicyHistoryView({ load, fixtureMode = FIXTURE_MODE, nameOf, onSelectAgent }: PolicyHistoryViewProps) {
  if (!load) return <div className="muted small">loading…</div>
  if (load.err !== null) return <div className="error small">{load.err}</div>
  if (load.missing && fixtureMode) return <div className="muted small">{FIXTURE_NO_POLICY}</div>
  return <PolicyHistoryList versions={load.versions ?? []} nameOf={nameOf} onSelectAgent={onSelectAgent} />
})

export interface PolicyHistoryProps {
  agentId: string
  nameOf?: (id: string) => string
  onSelectAgent?: (id: string) => void
  /**
   * Show the fixture empty state without asking: for a view with no policy route behind it at all. Fixture playback
   * does not need it: the api layer serves the recorded histories, and a 404 for an agent the recording lacks shows
   * the same empty state.
   */
  fixture?: boolean
}

/**
 * GET /api/agents/{id}/policy (newest first), refreshed like the agent detail, live or from the fixture recording.
 * A 404 means no policy yet (live) or none recorded (fixture playback).
 */
export const PolicyHistory = memo(function PolicyHistory({ agentId, nameOf, onSelectAgent, fixture = false }: PolicyHistoryProps) {
  const [loaded, setLoaded] = useState<PolicyLoad | null>(null)

  useEffect(() => {
    if (fixture) return
    let cancelled = false
    const load = () => {
      void fetchPolicyHistory(agentId).then((l) => {
        if (!cancelled) setLoaded(l)
      })
    }
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [agentId, fixture])

  if (fixture) return <div className="muted small">{FIXTURE_NO_POLICY}</div>
  return <PolicyHistoryView load={loaded && loaded.id === agentId ? loaded : null} nameOf={nameOf} onSelectAgent={onSelectAgent} />
})
