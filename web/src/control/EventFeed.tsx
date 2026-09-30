import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { EventMsg, RosterAgent } from '../protocol'
import { isEventKind } from '../protocol'
import { selectTierColor, useStore } from '../state/store'
import { fmtUsd } from './charts/chartTheme'

const ROW_H = 28
const VIEW_H = 336
const OVERSCAN = 14
const STICK_PX = 40

export const FEED_KINDS = [
  'talk',
  'forage',
  'transfer',
  'degeneration',
  'gossip_transfer',
  'claim',
  'birth',
  'death',
  'windfall',
  'epoch',
  'gadget_verified',
  'gadget_rejected',
  'sleep',
  'revise_self',
  'task_posted',
  'task_assigned',
  'task_completed',
  'apply_task',
  'benefactor_grant',
  'move',
  'idle',
  'remember',
  'share_note',
  'read_chronicle',
  'use_gadget',
  'action_failed',
  'gate_blocked',
  'weather',
  'tick',
] as const

function nameOf(id: string | null | undefined, roster: ReadonlyMap<string, RosterAgent>): string {
  if (!id) return '—'
  return roster.get(id)?.name ?? id
}

/** A claim is either a string or the kernel's structured `{subject, id, attr, value}`. */
export function claimText(c: unknown): string {
  if (typeof c === 'string') return c
  if (c && typeof c === 'object') {
    const o = c as Record<string, unknown>
    const subject = typeof o.subject === 'string' ? o.subject : ''
    const id = typeof o.id === 'string' ? o.id : ''
    const rawAttr = typeof o.attr === 'string' ? o.attr : ''
    const attr = (rawAttr.startsWith(subject + '_') ? rawAttr.slice(subject.length + 1) : rawAttr).replace(/_/g, ' ')
    const value = o.value === undefined || o.value === null ? '' : String(o.value)
    const head = [subject, id].filter(Boolean).join(' ')
    return `${head}${head && attr ? ' ' : ''}${attr}${value ? ` = ${value}` : ''}`
  }
  return String(c ?? '')
}

/** One-line, text-only summary of an event (agent strings stay literal). */
export function summarize(e: EventMsg, roster: ReadonlyMap<string, RosterAgent>): string {
  const n = (id: string | null | undefined) => nameOf(id, roster)
  if (isEventKind(e, 'talk')) return `${n(e.payload.speaker_id)} → ${n(e.payload.listener_id)}: ${e.payload.text}`
  if (isEventKind(e, 'forage')) return `${n(e.payload.agent_id)} foraged ${e.payload.units} at ${e.payload.node_id} (+${fmtUsd(e.payload.usd)})`
  if (isEventKind(e, 'transfer')) return `${n(e.payload.from)} → ${n(e.payload.to)} ${fmtUsd(e.payload.amount_usd)}`
  if (isEventKind(e, 'degeneration')) return `${e.payload.agent_name} degenerated (${e.payload.mode}) T_eff ${e.payload.t_eff.toFixed(2)} ≥ T_c ${e.payload.t_c.toFixed(2)}`
  if (isEventKind(e, 'gossip_transfer')) return `${n(e.payload.speaker_id)} → ${n(e.payload.listener_id)} “${e.payload.note_title}” hop ${e.payload.hop} · sim ${e.payload.similarity.toFixed(2)}`
  if (isEventKind(e, 'birth')) return `${e.payload.name} born (${e.payload.kind}, g${e.payload.generation}, ${e.payload.tier}) endowed ${fmtUsd(e.payload.endowment_usd)}`
  if (isEventKind(e, 'death')) return `${e.payload.name} died (${e.payload.cause}, g${e.payload.generation})${e.payload.replacement_id ? ` → ${n(e.payload.replacement_id)}` : ''}`
  if (isEventKind(e, 'windfall')) return `${n(e.payload.agent_id)} received ${fmtUsd(e.payload.amount_usd)} from no recorded source`
  if (isEventKind(e, 'claim')) return `${n(e.payload.agent_id)} claims “${claimText(e.payload.claim)}” — ${e.payload.truthful ? 'true' : 'false'}`
  if (isEventKind(e, 'epoch')) return `${e.payload.kind} ${e.payload.phase ?? 'started'}${typeof e.payload.scarcity === 'number' ? ` · scarcity ${e.payload.scarcity}` : ''}${typeof e.payload.weather_baseline === 'number' ? ` · weather ${e.payload.weather_baseline}` : ''}${typeof e.payload.ends_day === 'number' ? ` · until day ${e.payload.ends_day}` : ''}`
  if (isEventKind(e, 'gadget_verified')) return `${e.payload.name} verified for ${n(e.payload.owner)} (${e.payload.stage})`
  if (isEventKind(e, 'gadget_rejected')) return `${e.payload.name} rejected at ${e.payload.stage}: ${e.payload.reason ?? '—'}`
  const p = e.payload as Record<string, unknown>
  const parts: string[] = []
  for (const k of Object.keys(p).slice(0, 4)) {
    const v = p[k]
    if (v === null || v === undefined) continue
    parts.push(`${k}=${typeof v === 'object' ? '…' : String(v)}`)
  }
  return `${n(e.agent_id)} ${parts.join(' ')}`
}

const Row = memo(function Row({ e, y, roster, color, onSelect }: { e: EventMsg; y: number; roster: ReadonlyMap<string, RosterAgent>; color: string; onSelect: (id: string | null) => void }) {
  const text = summarize(e, roster)
  return (
    <div className={'feed-row' + (e.visibility === 'operator' ? ' operator' : '')} style={{ transform: `translateY(${y}px)` }} title={text}>
      <span className="feed-tick tnum">{e.tick}</span>
      <span className="feed-kind">{e.kind}</span>
      <span className="feed-dot" style={{ background: color }} onClick={() => onSelect(e.agent_id)} />
      <span className="feed-text">{text}</span>
    </div>
  )
})

export const EventFeed = memo(function EventFeed() {
  const ring = useStore((s) => s.events)
  const rev = useStore((s) => s.eventsRev)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const { filter, setFilter, select } = useStore(useShallow((s) => ({ filter: s.filter, setFilter: s.setFilter, select: s.select })))
  const roster = useStore((s) => s.roster)

  const scroller = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [stuck, setStuck] = useState(true)
  const seenCount = useRef(0)
  const [newCount, setNewCount] = useState(0)

  const total = ring.filteredCount
  const totalH = total * ROW_H

  // Stick to bottom when the reader is within 40 px of it; otherwise count new rows.
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (stuck) {
      el.scrollTop = totalH
      seenCount.current = total
      setNewCount(0)
    } else {
      setNewCount(Math.max(0, total - seenCount.current))
    }
  }, [rev, total, totalH, stuck])

  const onScroll = useCallback(() => {
    const el = scroller.current
    if (!el) return
    setScrollTop(el.scrollTop)
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
    setStuck(nearBottom)
    if (nearBottom) {
      seenCount.current = total
      setNewCount(0)
    }
  }, [total])

  const jump = useCallback(() => {
    const el = scroller.current
    if (el) el.scrollTop = el.scrollHeight
    setStuck(true)
  }, [])

  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scrollTop + VIEW_H) / ROW_H) + OVERSCAN)
  const rows: Array<{ e: EventMsg; i: number }> = []
  for (let i = start; i < end; i++) {
    const e = ring.filteredAt(i)
    if (e) rows.push({ e, i })
  }

  const kindSet = useMemo(() => new Set(filter.kinds ?? []), [filter.kinds])
  const toggleKind = useCallback(
    (k: string) => {
      const next = new Set(filter.kinds ?? [])
      if (filter.kinds === null) {
        next.clear()
        next.add(k)
      } else if (next.has(k)) next.delete(k)
      else next.add(k)
      setFilter({ kinds: next.size === 0 ? null : Array.from(next), agentId: filter.agentId })
    },
    [filter, setFilter],
  )

  useEffect(() => {
    // filter change resets the view to the bottom
    setStuck(true)
  }, [filter])

  return (
    <div className="feed">
      <div className="feed-filters">
        <button type="button" className={'chip' + (filter.kinds === null ? ' active' : '')} onClick={() => setFilter({ kinds: null, agentId: filter.agentId })}>
          all
        </button>
        {FEED_KINDS.map((k) => (
          <button key={k} type="button" className={'chip' + (kindSet.has(k) ? ' active' : '')} onClick={() => toggleKind(k)}>
            {k}
          </button>
        ))}
        <select className="select" value={filter.agentId ?? ''} onChange={(e) => setFilter({ kinds: filter.kinds, agentId: e.currentTarget.value || null })} aria-label="agent filter">
          <option value="">every agent</option>
          {roster.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <div className="feed-scroll" ref={scroller} onScroll={onScroll} style={{ height: VIEW_H }}>
        <div className="feed-spacer" style={{ height: totalH }}>
          {rows.map(({ e, i }) => (
            <Row key={e.seq} e={e} y={i * ROW_H} roster={rosterById} color={selectTierColor(config, rosterById.get(e.agent_id ?? '')?.tier ?? '')} onSelect={select} />
          ))}
        </div>
        {total === 0 ? <div className="muted small feed-empty">No events match.</div> : null}
      </div>
      {newCount > 0 && !stuck ? (
        <button type="button" className="new-pill" onClick={jump}>
          {newCount} new ↓
        </button>
      ) : null}
    </div>
  )
})
