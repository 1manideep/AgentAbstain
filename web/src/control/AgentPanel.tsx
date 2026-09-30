import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { AgentDetail, NoteRecord } from '../protocol'
import { apiGet } from '../net/api'
import { cameraCommands } from '../scene/sceneState'
import { selectTierColor, useStore } from '../state/store'
import { fmtNum, fmtUsd, SERIES } from './charts/chartTheme'
import { Sparkline } from './charts/Sparkline'
import { NoteText } from './NoteText'
import { wordDiff } from './wordDiff'

const REFRESH_MS = 20000

const SelfHistory = memo(function SelfHistory({ versions }: { versions: AgentDetail['self_versions'] }) {
  const [open, setOpen] = useState<number | null>(null)
  const sorted = useMemo(() => [...versions].sort((a, b) => b.version - a.version), [versions])
  if (sorted.length === 0) return <div className="muted small">No self summary recorded.</div>
  return (
    <ol className="self-versions">
      {sorted.map((v, i) => {
        const prev = sorted[i + 1]
        const isOpen = open === v.version || (open === null && i === 0)
        return (
          <li key={v.version} className={'self-version' + (isOpen ? ' open' : '')}>
            <button type="button" className="self-head" onClick={() => setOpen(isOpen && open !== null ? -1 : v.version)}>
              <span className="tnum">v{v.version}</span>
              <span className="muted small tnum">tick {v.tick}</span>
              {prev ? <span className="muted small">diff vs v{prev.version}</span> : <span className="muted small">first</span>}
            </button>
            {isOpen ? (
              <p className="self-text">
                {prev
                  ? wordDiff(prev.summary, v.summary).map((t, k) => (
                      <span key={k} className={'diff-' + t.kind}>
                        {t.text}{' '}
                      </span>
                    ))
                  : v.summary}
              </p>
            ) : null}
          </li>
        )
      })}
    </ol>
  )
})

const Notes = memo(function Notes({ notes }: { notes: NoteRecord[] }) {
  const [active, setActive] = useState<string | null>(null)
  const refs = useRef(new Map<string, HTMLLIElement>())
  const byTitle = useMemo(() => {
    const m = new Map<string, NoteRecord>()
    for (const n of notes) m.set(n.title.trim().toLowerCase(), n)
    return m
  }, [notes])
  const resolves = useCallback((t: string) => byTitle.has(t.trim().toLowerCase()), [byTitle])
  const onLink = useCallback(
    (t: string) => {
      const n = byTitle.get(t.trim().toLowerCase())
      if (!n) return
      setActive(n.note_id)
      refs.current.get(n.note_id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    },
    [byTitle],
  )
  if (notes.length === 0) return <div className="muted small">No notes.</div>
  return (
    <ul className="notes">
      {notes.map((n) => (
        <li
          key={n.note_id}
          className={'note' + (active === n.note_id ? ' active' : '') + (n.archived ? ' archived' : '')}
          ref={(el) => {
            if (el) refs.current.set(n.note_id, el)
            else refs.current.delete(n.note_id)
          }}
          onClick={() => setActive(n.note_id)}
        >
          <div className="note-head">
            <span className="note-title">{n.title}</span>
            <span className={'tag ' + n.channel}>{n.channel}</span>
            {n.hop > 0 ? <span className="muted small">hop {n.hop}</span> : null}
            <span className="muted small tnum">t{n.created_tick}</span>
          </div>
          <div className="note-body">
            <NoteText text={n.body} onLink={onLink} resolves={resolves} />
          </div>
        </li>
      ))}
    </ul>
  )
})

export const AgentPanel = memo(function AgentPanel() {
  const id = useStore((s) => s.selectedAgentId)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const statsRev = useStore((s) => s.statsRev)
  const agentStats = useStore((s) => s.agentStats)
  const { select, follow, followId } = useStore(useShallow((s) => ({ select: s.select, follow: s.follow, followId: s.followAgentId })))
  const [loaded, setLoaded] = useState<{ id: string; detail: AgentDetail | null; err: string | null } | null>(null)
  const detail = loaded && loaded.id === id ? loaded.detail : null
  const err = loaded && loaded.id === id ? loaded.err : null

  useEffect(() => {
    if (!id) return
    let cancelled = false
    const load = () => {
      apiGet<AgentDetail>(`/api/agents/${encodeURIComponent(id)}`)
        .then((d) => {
          if (!cancelled) setLoaded({ id, detail: d, err: null })
        })
        .catch((e: unknown) => {
          if (!cancelled) setLoaded({ id, detail: null, err: e instanceof Error ? e.message : String(e) })
        })
    }
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [id])

  const stat = id ? agentStats.get(id) : undefined
  void statsRev
  const agent = id ? rosterById.get(id) : undefined
  const costs = useMemo(() => (detail ? detail.calls.map((c) => c.real_cost_usd) : []), [detail])
  const balances = useMemo(() => (detail ? detail.balance_series.map((b) => b.balance_usd) : []), [detail])
  const fmtCost = useCallback((v: number) => fmtUsd(v, 4), [])
  const fmtBal = useCallback((v: number) => fmtUsd(v, 3), [])

  if (!id) return <div className="muted small">Click an agent in the scene (double-click to follow). Esc clears.</div>
  const color = selectTierColor(config, agent?.tier ?? '')
  const rankOf = agent ? config?.tiers[agent.tier]?.rank : undefined
  const ladderSize = config?.intelligence?.ladder.length ?? 0
  const rung = typeof rankOf === 'number' && ladderSize > 0 ? { rank: rankOf, of: ladderSize } : null
  const parent = agent?.parent_id ? rosterById.get(agent.parent_id) : undefined
  return (
    <div className="agent-panel">
      <div className="agent-head">
        <span className="tree-dot big" style={{ background: color }} />
        <span className="agent-name">{agent?.name ?? id}</span>
        <span className="muted" title={rung ? `capability ladder rung ${rung.rank + 1} of ${rung.of}` : undefined}>
          {agent?.tier ?? '?'}
          {rung ? ` · rung ${rung.rank + 1}/${rung.of}` : ''} · g{agent?.generation ?? '?'}
        </span>
        <span className={'tag ' + (agent?.status ?? 'unknown')}>{agent?.status ?? 'unknown'}</span>
        <span className="spacer" />
        <button type="button" className={'btn tiny' + (followId === id ? ' active' : '')} onClick={() => follow(followId === id ? null : id)}>
          {followId === id ? 'following' : 'follow'}
        </button>
        <button type="button" className="btn tiny" onClick={() => (cameraCommands.frameRequested = true)} title="frame population (F)">
          frame
        </button>
        <button type="button" className="btn tiny" onClick={() => select(null)} title="clear selection (Esc)">
          ✕
        </button>
      </div>
      <dl className="kv">
        <dt>parent</dt>
        <dd>
          {parent ? (
            <span className="wikilink" role="button" tabIndex={0} onClick={() => select(parent.id)}>
              {parent.name}
            </span>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>
        <dt>balance</dt>
        <dd className="tnum">{stat ? fmtUsd(stat.balance) : '—'}</dd>
        <dt>stress</dt>
        <dd className="tnum">
          {stat ? fmtNum(stat.stress) : '—'} <span className="muted">· T_eff {stat ? fmtNum(stat.tEff) : '—'}</span>
        </dd>
        <dt>last action</dt>
        <dd>
          {stat?.lastAction ? (
            <>
              {stat.lastAction.type}
              {stat.lastAction.target ? <span className="muted"> → {rosterById.get(stat.lastAction.target)?.name ?? stat.lastAction.target}</span> : null}
              <span className={stat.lastAction.ok ? 'ok-text' : 'bad-text'}> {stat.lastAction.ok ? 'ok' : 'failed'}</span>
            </>
          ) : (
            '—'
          )}
        </dd>
        <dt>state</dt>
        <dd>
          {stat?.asleep ? 'asleep' : stat?.degenerate ? 'degenerate' : (stat?.anim ?? '—')}
          {stat?.effects.length ? <span className="muted"> · effects: {stat.effects.join(', ')}</span> : null}
        </dd>
        <dt>born</dt>
        <dd className="tnum">
          tick {agent?.born_tick ?? '—'}
          {agent?.died_tick != null ? ` · died ${agent.died_tick}` : ''}
        </dd>
      </dl>

      <h3 className="sub">Call history · cost per call</h3>
      <Sparkline values={costs} color={SERIES[0]} format={fmtCost} />
      <h3 className="sub">Balance</h3>
      <Sparkline values={balances} color={color} format={fmtBal} />

      <h3 className="sub">Self summary · versions</h3>
      {detail ? <SelfHistory versions={detail.self_versions} /> : err ? <div className="error small">{err}</div> : <div className="muted small">loading…</div>}

      <h3 className="sub">Notes</h3>
      {detail ? <Notes notes={detail.notes} /> : null}
    </div>
  )
})
