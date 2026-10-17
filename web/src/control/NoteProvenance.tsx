import { Fragment, memo, useEffect, useState } from 'react'
import type { NoteDetail } from '../protocol'
import { apiGet, FIXTURE_MODE } from '../net/api'
import { AgentLink } from './PolicyHistory'

export interface NoteProvenanceViewProps {
  detail: NoteDetail
  /** Display name for an agent id (the roster's name); defaults to the id. */
  nameOf?: (id: string) => string
  /** Selects an agent (the store's `select`). */
  onSelectAgent?: (id: string) => void
  /** Opens a linked note of the same agent (the panel scrolls to it). */
  onLink?: (target: string) => void
  /** Link targets that resolve to one of the agent's notes; others render inert. */
  resolves?: (target: string) => boolean
}

const dash = <span className="muted">none</span>

/**
 * Where a transferred note came from (PLAN §4.7): source agent, origin note and generation, the tick it was
 * transferred, the agents it passed through on the way to its holder, and the notes it links to. Every value is
 * agent- or server-provided text rendered as text nodes; agents are role=button spans, never hrefs.
 */
export const NoteProvenanceView = memo(function NoteProvenanceView({
  detail,
  nameOf = (id) => id,
  onSelectAgent,
  onLink,
  resolves,
}: NoteProvenanceViewProps) {
  const path = Array.isArray(detail.path_agents) ? detail.path_agents : []
  const links = Array.isArray(detail.links) ? detail.links : []
  return (
    <dl className="kv provenance" data-note-id={detail.note_id} onClick={(e) => e.stopPropagation()}>
      <dt>source</dt>
      <dd>{detail.source_agent_id ? <AgentLink id={detail.source_agent_id} name={nameOf(detail.source_agent_id)} onSelect={onSelectAgent} /> : dash}</dd>
      <dt>origin note</dt>
      <dd className="tnum">{detail.origin_note_id ?? dash}</dd>
      <dt>origin gen</dt>
      <dd className="tnum">{detail.origin_generation != null ? `g${detail.origin_generation}` : dash}</dd>
      <dt>transferred</dt>
      <dd className="tnum">
        {detail.transfer_tick != null ? `tick ${detail.transfer_tick}` : dash}
        <span className="muted">
          {' '}
          · {detail.channel} · hop {detail.hop}
        </span>
      </dd>
      <dt>path</dt>
      <dd>
        {path.length === 0
          ? dash
          : path.map((a, i) => (
              <Fragment key={i}>
                {i > 0 ? ' → ' : null}
                <AgentLink id={a} name={nameOf(a)} onSelect={onSelectAgent} />
              </Fragment>
            ))}
        {path.length > 0 ? <span className="muted"> → {nameOf(detail.agent_id)}</span> : null}
      </dd>
      <dt>links</dt>
      <dd>
        {links.length === 0
          ? dash
          : links.map((t, i) => {
              const ok = resolves ? resolves(t) : false
              return (
                <Fragment key={i}>
                  {i > 0 ? ', ' : null}
                  <span
                    className={'wikilink' + (ok && onLink ? '' : ' unresolved')}
                    role={ok && onLink ? 'button' : undefined}
                    tabIndex={ok && onLink ? 0 : undefined}
                    data-target={t}
                    onClick={ok && onLink ? () => onLink(t) : undefined}
                    onKeyDown={
                      ok && onLink
                        ? (e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              onLink(t)
                            }
                          }
                        : undefined
                    }
                  >
                    {t}
                  </span>
                </Fragment>
              )
            })}
      </dd>
      <dt>file</dt>
      <dd className="muted small" title={detail.path}>
        {detail.path || dash}
      </dd>
    </dl>
  )
})

export interface NoteProvenanceProps extends Omit<NoteProvenanceViewProps, 'detail'> {
  agentId: string
  noteId: string
  /** Fixture playback has no note endpoint; defaults to the build's feed mode. */
  fixture?: boolean
}

interface Loaded {
  key: string
  detail: NoteDetail | null
  err: string | null
}

/** Fetches GET /api/agents/{id}/notes/{note_id} when a transferred note is opened and shows its provenance. */
export const NoteProvenance = memo(function NoteProvenance({ agentId, noteId, fixture = FIXTURE_MODE, ...view }: NoteProvenanceProps) {
  const key = `${agentId}\u0000${noteId}`
  const [loaded, setLoaded] = useState<Loaded | null>(null)

  useEffect(() => {
    if (fixture) return
    let cancelled = false
    apiGet<NoteDetail>(`/api/agents/${encodeURIComponent(agentId)}/notes/${encodeURIComponent(noteId)}`)
      .then((d) => {
        if (!cancelled) setLoaded({ key, detail: d, err: null })
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoaded({ key, detail: null, err: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [agentId, noteId, key, fixture])

  if (fixture) return <div className="muted small">Provenance is not recorded in fixture playback.</div>
  const mine = loaded && loaded.key === key ? loaded : null
  if (!mine) return <div className="muted small">loading provenance…</div>
  if (mine.err !== null || !mine.detail) return <div className="error small">{mine.err ?? 'no provenance'}</div>
  return <NoteProvenanceView detail={mine.detail} {...view} />
})
