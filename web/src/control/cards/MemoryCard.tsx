import { memo, useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent } from 'react'
import { ApiError, apiGet } from '../../net/api'
import type { DayRow, MemorySummaryItem, MemorySummaryResponse, RosterAgent } from '../../protocol'
import type { ExamTally, MemorySeries } from '../../state/series'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { fmtPct, fmtUsd } from '../charts/chartTheme'
import { EmptyState } from './EmptyState'

/**
 * Memory spend and audit-exam scores (MEMORY_EVOLUTION §5.10 "exam scores and memory spend per agent", §7.1 "Audit
 * exam accuracy" and "Memory spend share"). Live figures come from the series' memory accumulators (maintenance,
 * maintenance_skipped and exam_answered events) and the day rows' maintenance and exam reports; GET
 * /api/memory/summary adds each agent's decide cost, which the share needs, and the database totals of a run the
 * page joined late. Registered only on runs with the memory layer enabled.
 */

const SUMMARY_PATH = '/api/memory/summary'
const SUMMARY_REFRESH_MS = 15_000
/** Day columns of the per-day tables: the newest days. */
export const DAY_COLUMNS = 6

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const ratio = (part: number, whole: number): number | null => (whole > 0 ? part / whole : null)
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

// ------------------------------------------------------------------ derived rows

export interface MemoryAgentRow {
  id: string
  name: string
  tier: string | null
  /** From the roster, else the summary; true when neither knows the agent. */
  alive: boolean
  /** Maintenance cost in USD, whoever paid. */
  maintenanceReal: number
  maintenanceWorld: number
  maintenanceCalls: number
  /** maintenance_skipped events seen for the agent. */
  skipped: number
  /** Decide-call real cost from the memory summary; null without one. */
  decideReal: number | null
  /** maintenance / (maintenance + decide), real cost, both from the same summary; null without one or without spend. */
  share: number | null
  /** Graded audit items (failed calls are kept out, as the server's ExamReport does). */
  asked: number
  correct: number
  abstained: number
  failed: number
  accuracy: number | null
  abstainRate: number | null
}

export interface MemoryTotals {
  maintenanceReal: number
  maintenanceWorld: number
  skipped: number
  /** Over every agent the summary lists, the dead included; null without a summary. */
  share: number | null
  asked: number
  correct: number
  abstained: number
  failed: number
  accuracy: number | null
  abstainRate: number | null
}

const byName = (x: MemoryAgentRow, y: MemoryAgentRow) => Number(y.alive) - Number(x.alive) || x.name.localeCompare(y.name) || (x.id < y.id ? -1 : x.id > y.id ? 1 : 0)

/**
 * One row per agent with maintenance or audit figures, plus every living agent the summary lists (its decide cost
 * alone gives a share), living agents first. The live accumulators and the summary are both
 * lower bounds of the same running totals (the page may have joined late; the summary may be a few seconds old), so
 * maintenance cost takes the larger of the two, and the exam tally comes whole from whichever graded more items.
 * The spend share uses the summary's maintenance and decide cost together, so both sides are read at one moment.
 */
// oxlint-disable-next-line react/only-export-components
export function memoryAgentRows(mem: MemorySeries, rosterById: ReadonlyMap<string, RosterAgent>, summary: readonly MemorySummaryItem[] | null): MemoryAgentRow[] {
  const sum = new Map<string, MemorySummaryItem>()
  for (const s of summary ?? []) if (s && typeof s.agent_id === 'string') sum.set(s.agent_id, s)
  const ids = new Set<string>(mem.byAgent.keys())
  for (const [id, s] of sum) {
    const active = (num(s.maintenance_real_usd) ?? 0) > 0 || (num(s.maintenance_world_usd) ?? 0) > 0 || (num(s.exam_asked) ?? 0) > 0
    if (active || s.alive === true || rosterById.get(id)?.status === 'alive') ids.add(id)
  }
  const rows: MemoryAgentRow[] = []
  for (const id of ids) {
    const a = mem.byAgent.get(id)
    const s = sum.get(id)
    const r = rosterById.get(id)
    const sMaint = num(s?.maintenance_real_usd) ?? 0
    const sDecide = s ? num(s.decide_real_usd) : null
    const sAsked = num(s?.exam_asked) ?? 0
    const fromSummary = s !== undefined && sAsked > (a?.asked ?? 0)
    const asked = fromSummary ? sAsked : (a?.asked ?? 0)
    const correct = fromSummary ? (num(s.exam_correct) ?? 0) : (a?.correct ?? 0)
    const abstained = fromSummary ? (num(s.exam_abstained) ?? 0) : (a?.abstained ?? 0)
    rows.push({
      id,
      name: r?.name ?? (typeof s?.name === 'string' && s.name ? s.name : id),
      tier: r?.tier ?? null,
      // an agent in neither the roster nor the summary is not dimmed: its status is unknown, not dead
      alive: r ? r.status === 'alive' : s ? s.alive === true : true,
      maintenanceReal: Math.max(a?.maintenanceReal ?? 0, sMaint),
      maintenanceWorld: Math.max(a?.maintenanceWorld ?? 0, num(s?.maintenance_world_usd) ?? 0),
      maintenanceCalls: a?.maintenanceCalls ?? 0,
      skipped: a?.maintenanceSkipped ?? 0,
      decideReal: sDecide,
      share: sDecide === null ? null : ratio(sMaint, sMaint + sDecide),
      asked,
      correct,
      abstained,
      failed: a?.failed ?? 0,
      accuracy: ratio(correct, asked),
      abstainRate: ratio(abstained, asked),
    })
  }
  return rows.sort(byName)
}

/** The run's line: sums of the rows, the run's skip count, and the share over every agent the summary lists. */
// oxlint-disable-next-line react/only-export-components
export function memoryTotals(rows: readonly MemoryAgentRow[], mem: MemorySeries, summary: readonly MemorySummaryItem[] | null): MemoryTotals {
  let maintenanceReal = 0
  let maintenanceWorld = 0
  let skipped = 0
  let asked = 0
  let correct = 0
  let abstained = 0
  let failed = 0
  for (const r of rows) {
    maintenanceReal += r.maintenanceReal
    maintenanceWorld += r.maintenanceWorld
    skipped += r.skipped
    asked += r.asked
    correct += r.correct
    abstained += r.abstained
    failed += r.failed
  }
  let share: number | null = null
  if (summary) {
    let m = 0
    let d = 0
    for (const s of summary) {
      m += num(s?.maintenance_real_usd) ?? 0
      d += num(s?.decide_real_usd) ?? 0
    }
    share = ratio(m, m + d)
  }
  return {
    maintenanceReal,
    maintenanceWorld,
    // skips without an agent id count in the run total only
    skipped: Math.max(skipped, mem.maintenanceSkipped),
    share,
    asked,
    correct,
    abstained,
    failed,
    accuracy: ratio(correct, asked),
    abstainRate: ratio(abstained, asked),
  }
}

export interface ExamGrid {
  /** Ascending, at most DAY_COLUMNS of the newest. */
  days: number[]
  kinds: string[]
  /** Tally per day, then per question kind. */
  byDay: Map<number, Map<string, ExamTally>>
}

/** The day row's exam report by kind (void/research/exams.py ExamReport.by_kind), or null without one. */
function reportedExam(row: DayRow | undefined): Map<string, ExamTally> | null {
  const ex = row?.exam
  if (!isObj(ex) || !isObj(ex.by_kind)) return null
  const out = new Map<string, ExamTally>()
  for (const [kind, t] of Object.entries(ex.by_kind)) {
    if (!isObj(t)) continue
    out.set(kind, { asked: num(t.asked) ?? 0, correct: num(t.correct) ?? 0, abstained: num(t.abstained) ?? 0, failed: num(t.failed) ?? 0 })
  }
  return out.size ? out : null
}

/** Audit tallies per question kind per day: the day row's report where it has arrived (the whole day), else the live events. */
// oxlint-disable-next-line react/only-export-components
export function examGrid(mem: MemorySeries, daily: readonly DayRow[], maxDays = DAY_COLUMNS): ExamGrid {
  const rows = new Map<number, DayRow>()
  for (const r of daily) rows.set(r.day, r)
  const all = new Set<number>(mem.examByDay.keys())
  for (const r of daily) if (reportedExam(r)) all.add(r.day)
  const days = [...all].sort((a, b) => a - b).slice(-maxDays)
  const byDay = new Map<number, Map<string, ExamTally>>()
  const kinds = new Set<string>()
  for (const day of days) {
    const t = reportedExam(rows.get(day)) ?? mem.examByDay.get(day) ?? new Map<string, ExamTally>()
    byDay.set(day, t)
    for (const k of t.keys()) kinds.add(k)
  }
  return { days, kinds: [...kinds].sort(), byDay }
}

export interface MaintenanceDay {
  day: number
  /** Agents maintained (the day report's `called`); null until the day row arrives. */
  called: number | null
  /** Maintenance holds the gate refused, every reason. */
  skipped: number
  /** Refused because the payer's balance could not cover the hold ('insufficient'); null until the day row arrives. */
  couldNotAfford: number | null
  /** Refused by the run's daily or total cap; null until the day row arrives. */
  capped: number | null
}

/**
 * Maintenance per day. The skip count is live (maintenance_skipped events); the reason split and the calls made come
 * with the day row's maintenance report (void/sim/maintenance.py MaintenanceReport: `called`, `skipped` by reason).
 */
// oxlint-disable-next-line react/only-export-components
export function maintenanceDays(mem: MemorySeries, daily: readonly DayRow[], maxDays = DAY_COLUMNS): MaintenanceDay[] {
  const rows = new Map<number, DayRow>()
  for (const r of daily) if (isObj(r.maintenance) && Object.keys(r.maintenance).length) rows.set(r.day, r)
  const days = [...new Set<number>([...mem.skippedByDay.keys(), ...rows.keys()])].sort((a, b) => a - b).slice(-maxDays)
  return days.map((day) => {
    const live = mem.skippedByDay.get(day) ?? 0
    const rep = rows.get(day)?.maintenance
    if (!isObj(rep)) return { day, called: null, skipped: live, couldNotAfford: null, capped: null }
    const reasons = isObj(rep.skipped) ? rep.skipped : {}
    let total = 0
    for (const v of Object.values(reasons)) total += num(v) ?? 0
    const couldNotAfford = num(reasons.insufficient) ?? 0
    return { day, called: num(rep.called), skipped: Math.max(total, live), couldNotAfford, capped: total - couldNotAfford }
  })
}

// ------------------------------------------------------------------ summary fetch

export type SummaryStatus = 'idle' | 'ok' | 'unavailable'

interface SummaryState {
  runId: string | null
  items: MemorySummaryItem[] | null
  status: SummaryStatus
}

/**
 * Poll GET /api/memory/summary: now, then every `everyMs` after each answer. A server without the route (404) is not
 * asked again; any other failure is retried. Returns the stop function.
 */
// oxlint-disable-next-line react/only-export-components
export function pollMemorySummary(
  onOk: (items: MemorySummaryItem[]) => void,
  onFail: () => void,
  get: (path: string) => Promise<MemorySummaryResponse> = apiGet,
  everyMs = SUMMARY_REFRESH_MS,
): () => void {
  let live = true
  let timer: ReturnType<typeof setTimeout> | undefined
  const load = () => {
    get(SUMMARY_PATH)
      .then((r) => {
        if (!live) return
        onOk(Array.isArray(r?.agents) ? r.agents : [])
        timer = setTimeout(load, everyMs)
      })
      .catch((e: unknown) => {
        if (!live) return
        onFail()
        if (!(e instanceof ApiError && e.status === 404)) timer = setTimeout(load, everyMs)
      })
  }
  load()
  return () => {
    live = false
    if (timer !== undefined) clearTimeout(timer)
  }
}

/**
 * The memory summary of the current run, refreshed every 15 s. The decide cost moves every tick, so the share it
 * gives may trail by one refresh; a transient failure keeps the last good summary of the run. Fixture playback asks
 * the same api layer, which answers with the recording's summary (the end of the run), or 404 (unavailable) when the
 * recording has none.
 */
function useMemorySummary(runId: string | null): SummaryState {
  const [state, setState] = useState<SummaryState>({ runId: null, items: null, status: 'idle' })
  useEffect(() => {
    if (runId === null) return
    return pollMemorySummary(
      (items) => setState({ runId, items, status: 'ok' }),
      () => setState((prev) => (prev.runId === runId && prev.items ? prev : { runId, items: null, status: 'unavailable' })),
    )
  }, [runId])
  return state.runId === runId ? state : { runId, items: null, status: 'idle' }
}

// ------------------------------------------------------------------ view

const TABLE: CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 11, tableLayout: 'auto' }
const TH: CSSProperties = { textAlign: 'right', fontWeight: 600, color: 'var(--muted)', padding: '3px 3px', whiteSpace: 'nowrap' }
const TH_L: CSSProperties = { ...TH, textAlign: 'left' }
const TD: CSSProperties = { textAlign: 'right', padding: '2px 3px', whiteSpace: 'nowrap', borderTop: '1px solid var(--border)' }
const TD_L: CSSProperties = { ...TD, textAlign: 'left', maxWidth: 96, overflow: 'hidden', textOverflow: 'ellipsis' }
const FOOT: CSSProperties = { ...TD, color: 'var(--text-2)', fontWeight: 600 }
const FOOT_L: CSSProperties = { ...FOOT, textAlign: 'left' }
const SELECTED: CSSProperties = { background: 'rgba(57, 135, 229, 0.16)' }
const DEAD: CSSProperties = { opacity: 0.55 }

const usd = (v: number): string => fmtUsd(v, Math.abs(v) > 0 && Math.abs(v) < 0.01 ? 4 : 3)
const pct = (v: number | null): string => (v === null ? '—' : fmtPct(v))
const count = (v: number | null): string => (v === null ? '—' : String(Math.round(v)))

function examTitle(t: { asked: number; correct: number; abstained: number; failed: number }): string {
  const failed = t.failed ? `, ${t.failed} failed (ungraded)` : ''
  return `${t.correct}/${t.asked} correct, ${t.abstained} abstained${failed}`
}

function sumTallies(m: ReadonlyMap<string, ExamTally> | undefined): ExamTally {
  const out: ExamTally = { asked: 0, correct: 0, abstained: 0, failed: 0 }
  for (const t of m?.values() ?? []) {
    out.asked += t.asked
    out.correct += t.correct
    out.abstained += t.abstained
    out.failed += t.failed
  }
  return out
}

const AgentTable = memo(function AgentTable({
  rows,
  totals,
  selectedId,
  onSelect,
  colorOf,
}: {
  rows: readonly MemoryAgentRow[]
  totals: MemoryTotals
  selectedId: string | null
  onSelect: (id: string | null) => void
  colorOf: (tier: string | null) => string
}) {
  const pick = (id: string) => onSelect(id === selectedId ? null : id)
  const onKey = (e: KeyboardEvent, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      pick(id)
    }
  }
  return (
    <table style={TABLE}>
      <thead>
        <tr>
          <th style={TH_L}>Agent</th>
          <th style={TH} title="maintenance cost, real USD">
            Maint.
          </th>
          <th style={TH} title="maintenance cost, world USD">
            World
          </th>
          <th style={TH} title="memory spend share: maintenance / (maintenance + decide), real cost">
            Share
          </th>
          <th style={TH} title="audit exam accuracy: correct / graded items">
            Audit
          </th>
          <th style={TH} title="audit abstentions / graded items">
            Abst.
          </th>
          <th style={TH} title="maintenance skipped: the gate refused the hold">
            Skips
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} style={r.id === selectedId ? SELECTED : r.alive ? undefined : DEAD} title={r.alive ? undefined : 'not alive'}>
            <td style={TD_L}>
              <span className="legend-swatch" style={{ display: 'inline-block', marginRight: 5, verticalAlign: 'middle', background: colorOf(r.tier) }} />
              <span className="wikilink" role="button" tabIndex={0} onClick={() => pick(r.id)} onKeyDown={(e) => onKey(e, r.id)}>
                {r.name}
              </span>
            </td>
            <td style={TD} className="tnum" title={`${r.maintenanceCalls} maintenance calls seen`}>
              {usd(r.maintenanceReal)}
            </td>
            <td style={TD} className="tnum">
              {usd(r.maintenanceWorld)}
            </td>
            <td style={TD} className="tnum" title={r.decideReal === null ? 'needs the memory summary' : `decide ${usd(r.decideReal)}`}>
              {pct(r.share)}
            </td>
            <td style={TD} className="tnum" title={examTitle(r)}>
              {pct(r.accuracy)}
            </td>
            <td style={TD} className="tnum">
              {pct(r.abstainRate)}
            </td>
            <td style={TD} className="tnum">
              {r.skipped}
            </td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <td style={FOOT_L}>all</td>
          <td style={FOOT} className="tnum">
            {usd(totals.maintenanceReal)}
          </td>
          <td style={FOOT} className="tnum">
            {usd(totals.maintenanceWorld)}
          </td>
          <td style={FOOT} className="tnum">
            {pct(totals.share)}
          </td>
          <td style={FOOT} className="tnum" title={examTitle(totals)}>
            {pct(totals.accuracy)}
          </td>
          <td style={FOOT} className="tnum">
            {pct(totals.abstainRate)}
          </td>
          <td style={FOOT} className="tnum">
            {totals.skipped}
          </td>
        </tr>
      </tfoot>
    </table>
  )
})

const ExamTable = memo(function ExamTable({ grid }: { grid: ExamGrid }) {
  if (grid.days.length === 0) return <div className="muted small">No audit items graded yet.</div>
  const cell = (t: ExamTally | undefined, key: string, style: CSSProperties = TD) => (
    <td key={key} style={style} className="tnum" title={t ? examTitle(t) : undefined}>
      {t && t.asked > 0 ? fmtPct(t.correct / t.asked) : t && t.failed > 0 ? 'failed' : '—'}
    </td>
  )
  return (
    <table style={TABLE}>
      <thead>
        <tr>
          <th style={TH_L}>Kind</th>
          {grid.days.map((d) => (
            <th key={d} style={TH}>
              {`day ${d}`}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {grid.kinds.map((k) => (
          <tr key={k}>
            <td style={TD_L}>{k}</td>
            {grid.days.map((d) => cell(grid.byDay.get(d)?.get(k), String(d)))}
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <td style={FOOT_L}>all</td>
          {grid.days.map((d) => cell(sumTallies(grid.byDay.get(d)), String(d), FOOT))}
        </tr>
        <tr>
          <td style={{ ...TD_L, color: 'var(--muted)' }}>graded</td>
          {grid.days.map((d) => (
            <td key={d} style={{ ...TD, color: 'var(--muted)' }} className="tnum">
              {sumTallies(grid.byDay.get(d)).asked}
            </td>
          ))}
        </tr>
      </tfoot>
    </table>
  )
})

const MaintenanceTable = memo(function MaintenanceTable({ days }: { days: readonly MaintenanceDay[] }) {
  if (days.length === 0) return <div className="muted small">No maintenance reports yet.</div>
  const line = (label: string, title: string, pickValue: (m: MaintenanceDay) => string, style: CSSProperties = TD) => (
    <tr>
      {/* the label may wrap so six day columns fit the narrowest control room */}
      <td style={{ ...style, textAlign: 'left', whiteSpace: 'normal' }} title={title}>
        {label}
      </td>
      {days.map((m) => (
        <td key={m.day} style={style} className="tnum">
          {pickValue(m)}
        </td>
      ))}
    </tr>
  )
  return (
    <table style={TABLE}>
      <thead>
        <tr>
          <th style={TH_L} aria-label="maintenance" />
          {days.map((m) => (
            <th key={m.day} style={TH}>
              {`day ${m.day}`}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {line('maintained', 'agents whose maintenance call ran (from the day row)', (m) => count(m.called))}
        {line('skipped', 'maintenance holds the gate refused, every reason', (m) => String(m.skipped))}
        {line('could not afford', "the payer's balance could not cover the hold (from the day row)", (m) => count(m.couldNotAfford), FOOT)}
        {line('run caps', "the run's daily or total cap refused the hold (from the day row)", (m) => count(m.capped))}
      </tbody>
    </table>
  )
})

export interface MemoryBodyProps {
  rows: readonly MemoryAgentRow[]
  totals: MemoryTotals
  grid: ExamGrid
  maintenance: readonly MaintenanceDay[]
  summaryStatus: SummaryStatus
  selectedId: string | null
  onSelect: (id: string | null) => void
  colorOf: (tier: string | null) => string
}

/** The card's body from derived rows (the card itself reads the store and the summary). */
export const MemoryBody = memo(function MemoryBody({ rows, totals, grid, maintenance, summaryStatus, selectedId, onSelect, colorOf }: MemoryBodyProps) {
  if (rows.length === 0 && grid.days.length === 0 && maintenance.length === 0) return <EmptyState />
  const shareNote =
    summaryStatus === 'ok'
      ? 'Share: maintenance / (maintenance + decide), real cost, from the memory summary.'
      : summaryStatus === 'unavailable'
        ? 'Share needs the decide cost from /api/memory/summary, which is unavailable.'
        : 'Share needs the decide cost from /api/memory/summary (loading).'
  return (
    <>
      <div className="sub">Spend and audit per agent</div>
      {rows.length > 0 ? (
        <AgentTable rows={rows} totals={totals} selectedId={selectedId} onSelect={onSelect} colorOf={colorOf} />
      ) : (
        <div className="muted small">No agent has maintenance or audit figures yet.</div>
      )}
      <div className="chart-sub" style={{ marginTop: 4 }}>
        {shareNote}
      </div>
      <div className="sub">Audit accuracy by kind and day</div>
      <ExamTable grid={grid} />
      <div className="sub">Maintenance by day</div>
      <MaintenanceTable days={maintenance} />
    </>
  )
})

export const MemoryCard = memo(function MemoryCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const runId = useStore((s) => s.runId)
  const selectedId = useStore((s) => s.selectedAgentId)
  const select = useStore((s) => s.select)
  const summary = useMemorySummary(runId)
  const view = useMemo(() => {
    void rev
    const rows = memoryAgentRows(series.memory, rosterById, summary.items)
    return {
      rows,
      totals: memoryTotals(rows, series.memory, summary.items),
      grid: examGrid(series.memory, series.daily),
      maintenance: maintenanceDays(series.memory, series.daily),
    }
  }, [rev, series, rosterById, summary.items])
  const colorOf = useMemo(() => (tier: string | null) => (tier === null ? 'var(--muted)' : selectTierColor(config, tier)), [config])
  const parts: string[] = []
  if (view.totals.accuracy !== null) parts.push(`audit ${fmtPct(view.totals.accuracy)}`)
  if (view.totals.share !== null) parts.push(`share ${fmtPct(view.totals.share, 1)}`)
  const meta = parts.length ? <span className="muted small tnum">{parts.join(' · ')}</span> : undefined
  return (
    <Card id="memory" title="Memory" meta={meta}>
      <MemoryBody
        rows={view.rows}
        totals={view.totals}
        grid={view.grid}
        maintenance={view.maintenance}
        summaryStatus={summary.status}
        selectedId={selectedId}
        onSelect={select}
        colorOf={colorOf}
      />
    </Card>
  )
})
