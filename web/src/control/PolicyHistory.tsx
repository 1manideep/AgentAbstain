import { memo, useEffect, useMemo, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import type { PolicyResponse, PolicyVersion } from '../protocol'
import { ApiError, apiGet, FIXTURE_MODE } from '../net/api'
import { fmtNum } from './charts/chartTheme'
import { wordDiff } from './wordDiff'

const REFRESH_MS = 20000

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
                <span className={'tag ' + v.source} data-source={v.source}>
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

export interface PolicyHistoryProps {
  agentId: string
  nameOf?: (id: string) => string
  onSelectAgent?: (id: string) => void
  /** Fixture playback has no policy endpoint; defaults to the build's feed mode. */
  fixture?: boolean
}

interface Loaded {
  id: string
  versions: PolicyVersion[] | null
  err: string | null
}

/** GET /api/agents/{id}/policy (newest first), refreshed like the agent detail. A 404 means no policy yet. */
export const PolicyHistory = memo(function PolicyHistory({ agentId, nameOf, onSelectAgent, fixture = FIXTURE_MODE }: PolicyHistoryProps) {
  const [loaded, setLoaded] = useState<Loaded | null>(null)

  useEffect(() => {
    if (fixture) return
    let cancelled = false
    const load = () => {
      apiGet<PolicyResponse>(`/api/agents/${encodeURIComponent(agentId)}/policy`)
        .then((r) => {
          if (!cancelled) setLoaded({ id: agentId, versions: Array.isArray(r?.versions) ? r.versions : [], err: null })
        })
        .catch((e: unknown) => {
          if (cancelled) return
          if (e instanceof ApiError && e.status === 404) setLoaded({ id: agentId, versions: [], err: null })
          else setLoaded({ id: agentId, versions: null, err: e instanceof Error ? e.message : String(e) })
        })
    }
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [agentId, fixture])

  if (fixture) return <div className="muted small">Policy history is not recorded in fixture playback.</div>
  const mine = loaded && loaded.id === agentId ? loaded : null
  if (!mine) return <div className="muted small">loading…</div>
  if (mine.err !== null) return <div className="error small">{mine.err}</div>
  return <PolicyHistoryList versions={mine.versions ?? []} nameOf={nameOf} onSelectAgent={onSelectAgent} />
})
