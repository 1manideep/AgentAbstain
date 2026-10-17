import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { AgentDetail, CallRecord, NoteRecord } from '../protocol'
import { apiGet } from '../net/api'
import { cameraCommands } from '../scene/sceneState'
import { selectTierColor, useStore } from '../state/store'
import { fmtNum, fmtUsd, SERIES } from './charts/chartTheme'
import { Sparkline } from './charts/Sparkline'
import { NoteProvenance } from './NoteProvenance'
import { NoteText } from './NoteText'
import { PolicyHistory } from './PolicyHistory'
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

/** Channels whose notes came from another agent; opening one fetches its provenance. */
const PROVENANCE_CHANNELS = new Set(['gossip', 'inherited', 'chronicle'])

interface NotesProps {
  agentId: string
  notes: NoteRecord[]
  nameOf: (id: string) => string
  onSelectAgent: (id: string) => void
}

const Notes = memo(function Notes({ agentId, notes, nameOf, onSelectAgent }: NotesProps) {
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
      {notes.map((n) => {
        const transferred = PROVENANCE_CHANNELS.has(n.channel)
        return (
          <li
            key={n.note_id}
            className={'note' + (active === n.note_id ? ' active' : '') + (n.archived ? ' archived' : '')}
            ref={(el) => {
              if (el) refs.current.set(n.note_id, el)
              else refs.current.delete(n.note_id)
            }}
            title={transferred ? 'click to see where this note came from' : undefined}
            onClick={(e) => {
              // a wikilink in the body already moved the selection to its target; do not take it back
              if (e.target instanceof Element && e.target.closest('.wikilink[role="button"]')) return
              setActive(n.note_id)
            }}
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
            {transferred && active === n.note_id ? (
              <NoteProvenance agentId={agentId} noteId={n.note_id} nameOf={nameOf} onSelectAgent={onSelectAgent} onLink={onLink} resolves={resolves} />
            ) : null}
          </li>
        )
      })}
    </ul>
  )
})

/** The purposes a call is metered under that the panel always lists; others (gossip, chronicle) follow when present. */
const CORE_PURPOSES = ['decide', 'maintain', 'exam']

interface PurposeCost {
  purpose: string
  calls: number
  realUsd: number
  worldUsd: number
}

/**
 * Cost of the agent's recent calls by purpose, or null when no call carries a purpose (a server that only
 * records decide calls). A call without a purpose counts as decide, the llm_calls default.
 */
function splitByPurpose(calls: CallRecord[]): PurposeCost[] | null {
  if (!calls.some((c) => typeof c.purpose === 'string' && c.purpose !== '')) return null
  const by = new Map<string, PurposeCost>(CORE_PURPOSES.map((p) => [p, { purpose: p, calls: 0, realUsd: 0, worldUsd: 0 }]))
  for (const c of calls) {
    const p = typeof c.purpose === 'string' && c.purpose !== '' ? c.purpose : 'decide'
    let row = by.get(p)
    if (!row) by.set(p, (row = { purpose: p, calls: 0, realUsd: 0, worldUsd: 0 }))
    row.calls += 1
    if (Number.isFinite(c.real_cost_usd)) row.realUsd += c.real_cost_usd
    const world = c.world_cost_usd ?? c.cost_usd
    if (typeof world === 'number' && Number.isFinite(world)) row.worldUsd += world
  }
  const core = CORE_PURPOSES.map((p) => by.get(p)!)
  const rest = [...by.values()].filter((r) => !CORE_PURPOSES.includes(r.purpose)).sort((a, b) => a.purpose.localeCompare(b.purpose))
  return [...core, ...rest]
}

const NUM_CELL = { textAlign: 'right', padding: '1px 0 1px 10px' } as const
const NAME_CELL = { textAlign: 'left', padding: '1px 0', fontWeight: 400 } as const

/**
 * The agent's recent call cost split by purpose (decide, maintain, exam, then any other purpose present), in
 * real and world dollars. When the calls carry no purpose the costs are labelled decide only.
 */
export const CallCostSplit = memo(function CallCostSplit({ calls }: { calls: CallRecord[] }) {
  const split = useMemo(() => splitByPurpose(calls), [calls])
  if (calls.length === 0) return null
  if (!split) {
    return (
      <div className="chart-sub" data-purpose="decide-only">
        decide only · {calls.length} calls · {fmtUsd(calls.reduce((s, c) => s + (Number.isFinite(c.real_cost_usd) ? c.real_cost_usd : 0), 0), 4)} real
      </div>
    )
  }
  const hasWorld = calls.some((c) => typeof (c.world_cost_usd ?? c.cost_usd) === 'number')
  return (
    <table className="cost-split small" style={{ borderCollapse: 'collapse', width: '100%', margin: '2px 0 6px' }}>
      <thead>
        <tr className="muted">
          <th style={NAME_CELL}>last {calls.length} calls</th>
          <th style={NUM_CELL}>calls</th>
          <th style={NUM_CELL}>real</th>
          {hasWorld ? <th style={NUM_CELL}>world</th> : null}
        </tr>
      </thead>
      <tbody>
        {split.map((r) => (
          <tr key={r.purpose} data-purpose={r.purpose} className={r.calls === 0 ? 'muted' : undefined}>
            <td style={NAME_CELL}>{r.purpose}</td>
            <td className="tnum" style={NUM_CELL}>
              {r.calls}
            </td>
            <td className="tnum" style={NUM_CELL}>
              {fmtUsd(r.realUsd, 4)}
            </td>
            {hasWorld ? (
              <td className="tnum" style={NUM_CELL}>
                {fmtUsd(r.worldUsd, 4)}
              </td>
            ) : null}
          </tr>
        ))}
      </tbody>
    </table>
  )
})

/** Server order of the six personality traits (void/agents/models.py TRAITS); unknown ones follow. */
const TRAIT_ORDER = ['greed', 'sociability', 'curiosity', 'caution', 'industriousness', 'honesty']

/** The roster's motto and personality traits (each in [0, 1], drawn as a bar). Nothing when the roster has neither. */
export const Persona = memo(function Persona({ motto, traits }: { motto?: string; traits?: Record<string, number> }) {
  const entries = useMemo(() => {
    const t = traits && typeof traits === 'object' ? traits : {}
    const keys = [...TRAIT_ORDER.filter((k) => k in t), ...Object.keys(t).filter((k) => !TRAIT_ORDER.includes(k))]
    return keys.map((k) => [k, t[k]] as const).filter((e): e is readonly [string, number] => typeof e[1] === 'number' && Number.isFinite(e[1]))
  }, [traits])
  const text = typeof motto === 'string' ? motto.trim() : ''
  if (!text && entries.length === 0) return null
  return (
    <>
      <h3 className="sub">Persona</h3>
      {text ? (
        <p className="motto" style={{ margin: '0 0 6px', fontStyle: 'italic', color: 'var(--text-2)' }}>
          “{text}”
        </p>
      ) : null}
      {entries.length > 0 ? (
        <div className="traits" style={{ display: 'grid', gridTemplateColumns: 'max-content 1fr 34px', gap: '2px 10px', alignItems: 'center' }}>
          {entries.map(([k, v]) => (
            <Fragment key={k}>
              <span className="muted">{k}</span>
              <span style={{ height: 6, borderRadius: 3, background: 'var(--surface-3)', overflow: 'hidden' }}>
                <span data-trait={k} style={{ display: 'block', height: '100%', width: `${Math.round(Math.min(1, Math.max(0, v)) * 100)}%`, background: SERIES[6] }} />
              </span>
              <span className="tnum small" style={{ textAlign: 'right' }}>
                {fmtNum(v, 2)}
              </span>
            </Fragment>
          ))}
        </div>
      ) : null}
    </>
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
  const nameOf = useCallback((aid: string) => rosterById.get(aid)?.name ?? aid, [rosterById])

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

      <Persona motto={agent?.motto} traits={agent?.traits} />

      <h3 className="sub">Call history · cost per call</h3>
      <Sparkline values={costs} color={SERIES[0]} format={fmtCost} />
      {detail ? <CallCostSplit calls={detail.calls} /> : null}
      <h3 className="sub">Balance</h3>
      <Sparkline values={balances} color={color} format={fmtBal} />

      <h3 className="sub">Self summary · versions</h3>
      {detail ? <SelfHistory versions={detail.self_versions} /> : err ? <div className="error small">{err}</div> : <div className="muted small">loading…</div>}

      {config?.memory_layer?.policy ? (
        <>
          <h3 className="sub">Memory policy · versions</h3>
          <PolicyHistory agentId={id} nameOf={nameOf} onSelectAgent={select} />
        </>
      ) : null}

      <h3 className="sub">Notes</h3>
      {detail ? <Notes key={id} agentId={id} notes={detail.notes} nameOf={nameOf} onSelectAgent={select} /> : null}
    </div>
  )
})
