import { memo, useEffect, useMemo } from 'react'
import { isEventKind, type DayRow, type EventMsg, type GadgetItem } from '../../protocol'
import { MAX_GATE_EVENTS } from '../../state/series'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { fmtPct, SERIES } from '../charts/chartTheme'
import { DayLines, type DayLineSeries } from '../charts/DailyCharts'
import { COUNTS_STALE_TITLE, countsAsOf, gadgetFailureRate, GadgetPanel, RegistryRefresh, selectGadget, useGadgetCounts, useRegistryView, useSelectedGadgetId } from '../GadgetPanel'
import { AgentLink } from '../PolicyHistory'
import { EmptyState } from './EmptyState'

// ---------------------------------------------------------------- proposals from the gate events

/** One gate outcome, from a gadget_verified or gadget_rejected event. */
export interface ProposalRow {
  seq: number
  tick: number
  gadgetId: string
  name: string
  owner: string
  verified: boolean
  /** "verified", or the stage that stopped the proposal (size, static, tests, spec, ...). */
  stage: string
  reason: string | null
  withoutTests: boolean
}

/** Gate outcomes the list keeps: the series keeps this many gate events for the whole run. */
export const MAX_PROPOSALS = MAX_GATE_EVENTS
/** Rows the list shows; older ones are counted, not drawn. */
const SHOWN = 40

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/**
 * The proposal rows of the series' gate events (`Series.gateEvents`: the run's newest MAX_GATE_EVENTS gadget_verified
 * and gadget_rejected events, seeded from the server's whole-run aggregate on a fresh load, so a proposal stays
 * listed after the 2000-event ring has moved past it), oldest first. Other kinds are skipped.
 */
// oxlint-disable-next-line react/only-export-components
export function proposalRows(events: readonly EventMsg[]): ProposalRow[] {
  const rows: ProposalRow[] = []
  for (const e of events) {
    if (!(isEventKind(e, 'gadget_verified') || isEventKind(e, 'gadget_rejected'))) continue
    const p = e.payload
    const verified = e.kind === 'gadget_verified'
    rows.push({
      seq: e.seq,
      tick: e.tick,
      gadgetId: str(p.gadget_id),
      name: str(p.name) || str(p.gadget_id),
      owner: str(p.owner) || str(e.agent_id),
      verified,
      stage: str(p.stage) || (verified ? 'verified' : 'unknown'),
      reason: typeof p.reason === 'string' && p.reason ? p.reason : null,
      withoutTests: p.verified_without_tests === true,
    })
  }
  return rows.length > MAX_PROPOSALS ? rows.slice(-MAX_PROPOSALS) : rows
}

// ---------------------------------------------------------------- daily failure rate

/** The day rows' gadget_use_failure_rate (cumulative failed / all uses at each day's end), in day order. */
// oxlint-disable-next-line react/only-export-components
export function dailyFailureRate(daily: readonly DayRow[]): { days: number[]; series: DayLineSeries[] } {
  const rows = [...daily].filter((r) => typeof r.day === 'number' && Number.isFinite(r.day)).sort((a, b) => a.day - b.day)
  const values = rows.map((r) => (typeof r.gadget_use_failure_rate === 'number' && Number.isFinite(r.gadget_use_failure_rate) ? r.gadget_use_failure_rate : null))
  return { days: rows.map((r) => r.day), series: [{ key: 'failure', label: 'use-failure rate', color: SERIES[0], values }] }
}

const fmtRate = (v: number) => fmtPct(v)

// ---------------------------------------------------------------- lists

const ROW = { display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap', padding: '2px 0' } as const
const LIST = { listStyle: 'none', margin: 0, padding: 0 } as const

interface RegistryProps {
  items: readonly GadgetItem[]
  selectedId: string | null
  nameOf: (id: string) => string
}

/** The verified gadgets on the map; clicking one opens its inspector. */
const RegistryList = memo(function RegistryList({ items, selectedId, nameOf }: RegistryProps) {
  if (items.length === 0) return <div className="muted small">No verified gadget on the map yet.</div>
  return (
    <ul className="gadget-registry small" style={LIST}>
      {items.map((g) => {
        const rate = gadgetFailureRate(g)
        return (
          <li key={g.id} data-gadget-id={g.id} style={ROW}>
            <span className="wikilink" role="button" tabIndex={0} aria-pressed={g.id === selectedId} style={g.id === selectedId ? { color: 'var(--text)' } : undefined}
              onClick={() => selectGadget(g.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  selectGadget(g.id)
                }
              }}
            >
              {g.name}
            </span>
            <span className="muted">{nameOf(g.owner)}</span>
            {g.effect_kind ? <span className="muted">{g.effect_kind}</span> : null}
            <span className="tnum muted">
              {g.uses} used{typeof g.failures === 'number' ? ` · ${g.failures} failed` : ''}
              {rate !== null ? ` (${fmtPct(rate)})` : ''}
            </span>
            {g.verified_without_tests ? (
              <span className="tag" style={{ color: 'var(--warning)' }}>
                untested
              </span>
            ) : null}
          </li>
        )
      })}
    </ul>
  )
})

interface ProposalsProps {
  rows: readonly ProposalRow[]
  nameOf: (id: string) => string
  onSelectAgent: (id: string) => void
  onMap: ReadonlySet<string>
}

/** Gate outcomes newest first: stage tag, gadget, owner and the reason a proposal stopped. */
export const ProposalList = memo(function ProposalList({ rows, nameOf, onSelectAgent, onMap }: ProposalsProps) {
  const shown = useMemo(() => [...rows].reverse().slice(0, SHOWN), [rows])
  if (rows.length === 0) return <div className="muted small">No gate outcome in the events received yet.</div>
  return (
    <>
      <ul className="gadget-proposals small" style={LIST}>
        {shown.map((r) => (
          <li key={r.seq} data-stage={r.stage} data-verified={r.verified ? '1' : '0'} style={ROW}>
            <span className="muted tnum">t{r.tick}</span>
            <span className={'tag ' + (r.verified ? 'approved' : 'rejected')}>{r.verified ? 'verified' : r.stage}</span>
            {r.verified && onMap.has(r.gadgetId) ? (
              <span className="wikilink" role="button" tabIndex={0} onClick={() => selectGadget(r.gadgetId)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    selectGadget(r.gadgetId)
                  }
                }}
              >
                {r.name}
              </span>
            ) : (
              <span>{r.name}</span>
            )}
            <AgentLink id={r.owner} name={nameOf(r.owner)} onSelect={onSelectAgent} />
            {r.withoutTests ? (
              <span className="tag" style={{ color: 'var(--warning)' }}>
                untested
              </span>
            ) : null}
            {r.reason ? (
              <span className="muted" style={{ flexBasis: '100%', paddingLeft: 4, wordBreak: 'break-word' }}>
                {r.reason}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      {rows.length > shown.length ? <div className="muted small">{rows.length - shown.length} older not shown</div> : null}
    </>
  )
})

/** "3 verified · 2 rejected (tests 1, static 1)" over the listed proposals. */
// oxlint-disable-next-line react/only-export-components
export function proposalSummary(rows: readonly ProposalRow[]): string {
  let verified = 0
  const stages = new Map<string, number>()
  for (const r of rows) {
    if (r.verified) verified++
    else stages.set(r.stage, (stages.get(r.stage) ?? 0) + 1)
  }
  const rejected = rows.length - verified
  const by = [...stages.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([s, n]) => `${s} ${n}`)
  return `${verified} verified · ${rejected} rejected${by.length ? ` (${by.join(', ')})` : ''}`
}

// ---------------------------------------------------------------- the card

/**
 * The gadget registry and the gate (DESIGN §12, §17 exp_gate): the selected gadget's inspector, the verified gadgets
 * on the map, the daily use-failure rate from the day rows and the verified and rejected proposals from the gate
 * events. While the card is open it refetches the registry so the uses and failures follow the run, and it says which
 * tick they are current at.
 */
export const GadgetsCard = memo(function GadgetsCard() {
  const seriesRev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  // the pushed registry, or a fresher one from GET /api/gadgets (RegistryRefresh, in the open card's body): the push
  // only follows new verifications
  const registry = useRegistryView()
  const refreshFailed = useGadgetCounts((s) => s.failed)
  const rosterById = useStore((s) => s.rosterById)
  const selectAgent = useStore((s) => s.select)
  const selectedId = useSelectedGadgetId()

  // the gate events change only with gateRev (every ingested event bumps seriesRev, which re-renders the card)
  const gateRev = series.gateRev
  const proposals = useMemo(() => {
    void gateRev
    return proposalRows(series.gateEvents)
  }, [series, gateRev])
  const daySignature = useMemo(() => {
    void seriesRev
    return series.daily.map((r) => `${r.day}:${r.gadget_use_failure_rate ?? ''}`).join(',')
  }, [series, seriesRev])
  const failure = useMemo(() => {
    void daySignature
    return dailyFailureRate(series.daily)
  }, [series, daySignature])
  const items = useMemo(() => registry.msg?.items ?? [], [registry])
  const onMap = useMemo(() => new Set(items.map((g) => g.id)), [items])
  const nameOf = useMemo(() => (id: string) => rosterById.get(id)?.name ?? id, [rosterById])

  useEffect(() => {
    if (!selectedId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      selectGadget(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selectedId])

  const hasRate = failure.series[0]!.values.some((v) => v !== null)
  const empty = !selectedId && items.length === 0 && proposals.length === 0 && !hasRate
  const selected = selectedId ? items.find((g) => g.id === selectedId) : undefined
  const meta = selected ? (
    <span className="muted small">{selected.name}</span>
  ) : items.length ? (
    <span className="muted small tnum">{items.length} on the map</span>
  ) : undefined
  return (
    <Card id="gadgets" title="Gadgets" meta={meta}>
      {empty ? (
        <EmptyState />
      ) : (
        <>
          <RegistryRefresh />
          <GadgetPanel />
          <h3 className="sub">Registry</h3>
          {items.length ? (
            <div className="chart-sub" data-field="counts-as-of" title={registry.asOfTick === null ? COUNTS_STALE_TITLE : undefined}>
              uses and failures {countsAsOf(registry.asOfTick)}
              {refreshFailed ? '; the last refresh failed' : ''}
            </div>
          ) : null}
          <RegistryList items={items} selectedId={selectedId} nameOf={nameOf} />
          <h3 className="sub">Use-failure rate by day</h3>
          <div className="chart-sub">failed / all gadget uses so far, at each day's end</div>
          <DayLines days={failure.days} series={failure.series} yMax={1} format={fmtRate} label="gadget use-failure rate per day" empty="No gadget uses yet." />
          <h3 className="sub">Proposals</h3>
          {proposals.length ? <div className="chart-sub">{proposalSummary(proposals)}</div> : null}
          <ProposalList rows={proposals} nameOf={nameOf} onSelectAgent={selectAgent} onMap={onMap} />
        </>
      )}
    </Card>
  )
})
