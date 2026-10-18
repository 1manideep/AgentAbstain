import { memo, useEffect, useMemo, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { ApiError, apiGet, FIXTURE_MODE } from '../../net/api'
import type { PracticeEventRow, PracticesResponse } from '../../protocol'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { ChartTip, useChartTip, type TipRow } from '../charts/ChartTip'
import { fmtPct, INK, linePath, niceTicks, SERIES } from '../charts/chartTheme'
import { EmptyState } from './EmptyState'

/**
 * Practice adoption curves (MEMORY_EVOLUTION §5.10 "adoption curves"; §7.1 "Adoption rate, latency, persistence | from
 * practice_events"). GET /api/practices lists the kernel's practice_events rows newest first; the card counts the
 * suggested, adopted, persisted and dropped events per day of their tick. Registered only on runs with the policy
 * file on.
 */

// oxlint-disable-next-line react/only-export-components
export const PRACTICE_KINDS = ['suggested', 'adopted', 'persisted', 'dropped'] as const
export type PracticeKind = (typeof PRACTICE_KINDS)[number]

// oxlint-disable-next-line react/only-export-components
export const KIND_COLORS: Readonly<Record<PracticeKind, string>> = {
  suggested: SERIES[0],
  adopted: SERIES[2],
  persisted: SERIES[6],
  dropped: SERIES[1],
}

/** The explicit empty state once the server has answered with no rows. */
export const ADOPTION_EMPTY = 'no practice transmission recorded'

const PRACTICES_LIMIT = 5000
export const PRACTICES_PATH = `/api/practices?limit=${PRACTICES_LIMIT}`
const REFRESH_MS = 20_000

const isKind = (k: unknown): k is PracticeKind => typeof k === 'string' && (PRACTICE_KINDS as readonly string[]).includes(k)

/** The clock's day of a tick: tick 1 is the first tick of day 1, tick 0 is before the run (void/economy/scheduler.py). */
// oxlint-disable-next-line react/only-export-components
export function dayOfTick(tick: number, ticksPerDay: number): number {
  if (!(tick > 0) || !(ticksPerDay > 0)) return 0
  return Math.floor((tick - 1) / ticksPerDay) + 1
}

export type AdoptionDay = { day: number } & Record<PracticeKind, number>

/**
 * Events per day and kind, in day order, every day from the first to the last with an event (a quiet day counts
 * zero, so the curves do not skip it). Rows of an unknown kind or without a finite tick are left out.
 */
// oxlint-disable-next-line react/only-export-components
export function adoptionDays(events: readonly PracticeEventRow[], ticksPerDay: number): AdoptionDay[] {
  const byDay = new Map<number, AdoptionDay>()
  for (const e of events) {
    if (!e || !isKind(e.kind) || typeof e.tick !== 'number' || !Number.isFinite(e.tick)) continue
    const day = dayOfTick(e.tick, ticksPerDay)
    let row = byDay.get(day)
    if (!row) {
      row = { day, suggested: 0, adopted: 0, persisted: 0, dropped: 0 }
      byDay.set(day, row)
    }
    row[e.kind]++
  }
  if (byDay.size === 0) return []
  const days = [...byDay.keys()]
  const lo = Math.min(...days)
  const hi = Math.max(...days)
  const out: AdoptionDay[] = []
  for (let d = lo; d <= hi; d++) out.push(byDay.get(d) ?? { day: d, suggested: 0, adopted: 0, persisted: 0, dropped: 0 })
  return out
}

export interface AdoptionTotals extends Record<PracticeKind, number> {
  /** adopted / suggested; null without a suggestion. */
  adoptionRate: number | null
  /** persisted / adopted; null without an adoption. A recent adoption has not had time to persist or drop yet. */
  persistence: number | null
}

// oxlint-disable-next-line react/only-export-components
export function adoptionTotals(days: readonly AdoptionDay[]): AdoptionTotals {
  const t = { suggested: 0, adopted: 0, persisted: 0, dropped: 0 }
  for (const d of days) for (const k of PRACTICE_KINDS) t[k] += d[k]
  return {
    ...t,
    adoptionRate: t.suggested > 0 ? t.adopted / t.suggested : null,
    persistence: t.adopted > 0 ? t.persisted / t.adopted : null,
  }
}

/**
 * Poll GET /api/practices: now, then every `everyMs` after each answer. A server without the route (404) is not asked
 * again; any other failure is retried. Returns the stop function.
 */
// oxlint-disable-next-line react/only-export-components
export function pollPractices(
  onOk: (events: PracticeEventRow[]) => void,
  onFail: () => void,
  get: (path: string) => Promise<PracticesResponse> = apiGet,
  everyMs = REFRESH_MS,
): () => void {
  let live = true
  let timer: ReturnType<typeof setTimeout> | undefined
  const load = () => {
    get(PRACTICES_PATH)
      .then((r) => {
        if (!live) return
        onOk(Array.isArray(r?.events) ? r.events : [])
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

// ------------------------------------------------------------------ chart

const W = 320
const H = 120
const PAD_L = 4
const PAD_R = 10
const TOP = 14
const BOTTOM = 16

export const AdoptionChart = memo(function AdoptionChart({ days }: { days: readonly AdoptionDay[] }) {
  const { tip, show, hide } = useChartTip()
  const [boxW, setBoxW] = useState(W)
  const [hoverDay, setHoverDay] = useState<number | null>(null)
  if (days.length === 0) return null
  const d0 = days[0]!.day
  const d1 = days[days.length - 1]!.day
  let dataMax = 0
  for (const d of days) for (const k of PRACTICE_KINDS) dataMax = Math.max(dataMax, d[k])
  const ticks = niceTicks(Math.max(1, dataMax), 2)
  const max = ticks[ticks.length - 1]!
  const plotW = W - PAD_L - PAD_R
  const plotH = H - TOP - BOTTOM
  const xOf = (d: number) => (d1 > d0 ? PAD_L + ((d - d0) / (d1 - d0)) * plotW : PAD_L + plotW / 2)
  const yOf = (v: number) => TOP + plotH - (Math.min(max, Math.max(0, v)) / max) * plotH
  const onMove = (e: ReactPointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (!(rect.width > 0)) return
    const vx = ((e.clientX - rect.left) / rect.width) * W
    let best = days[0]!
    for (const d of days) if (Math.abs(xOf(d.day) - vx) < Math.abs(xOf(best.day) - vx)) best = d
    const rows: TipRow[] = PRACTICE_KINDS.map((k) => ({ label: k, value: String(best[k]), color: KIND_COLORS[k] }))
    setBoxW(rect.width)
    setHoverDay(best.day)
    show((xOf(best.day) / W) * rect.width, (TOP / H) * rect.height, `Day ${best.day}`, rows)
  }
  const onLeave = () => {
    setHoverDay(null)
    hide()
  }
  return (
    <div className="chart" style={{ minWidth: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="practice events per day by kind">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD_L} x2={W - PAD_R} y1={yOf(t)} y2={yOf(t)} stroke={t === 0 ? INK.axis : INK.grid} strokeWidth="1" />
            {t > 0 ? (
              <text x={PAD_L} y={yOf(t) - 3} fill={INK.muted} fontSize="9" className="tnum">
                {String(t)}
              </text>
            ) : null}
          </g>
        ))}
        <text x={PAD_L} y={H - 3} fill={INK.muted} fontSize="9" className="tnum">
          {`day ${d0}`}
        </text>
        {d1 > d0 ? (
          <text x={W - PAD_R} y={H - 3} fill={INK.muted} fontSize="9" textAnchor="end" className="tnum">
            {`day ${d1}`}
          </text>
        ) : null}
        {hoverDay !== null ? <line x1={xOf(hoverDay)} x2={xOf(hoverDay)} y1={TOP} y2={TOP + plotH} stroke={INK.axis} strokeWidth="1" /> : null}
        {PRACTICE_KINDS.map((k) => {
          const pts = days.map((d): [number, number] => [xOf(d.day), yOf(d[k])])
          const last = pts[pts.length - 1]!
          return (
            <g key={k} data-kind={k}>
              {pts.length >= 2 ? (
                <path d={linePath(pts)} fill="none" stroke={KIND_COLORS[k]} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              ) : null}
              <circle cx={last[0]} cy={last[1]} r="3" fill={KIND_COLORS[k]} stroke={INK.surface} strokeWidth="1.5" />
            </g>
          )
        })}
        <rect x={PAD_L} y={TOP} width={plotW} height={plotH} fill="transparent" onPointerMove={onMove} onPointerLeave={onLeave} />
      </svg>
      <ChartTip tip={tip} width={boxW} />
    </div>
  )
})

// ------------------------------------------------------------------ body

export type PracticesStatus = 'idle' | 'ok' | 'unavailable'

const TABLE: CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 11 }
const TD: CSSProperties = { textAlign: 'right', padding: '2px 3px', whiteSpace: 'nowrap', borderTop: '1px solid var(--border)' }
const TD_L: CSSProperties = { ...TD, textAlign: 'left' }

export interface AdoptionBodyProps {
  days: readonly AdoptionDay[]
  status: PracticesStatus
  /** The answer held the full limit of rows, so the oldest days may be missing or partial. */
  truncated?: boolean
  fixture?: boolean
}

/** The card's body from derived rows (the card itself polls the endpoint). */
export const AdoptionBody = memo(function AdoptionBody({ days, status, truncated = false, fixture = false }: AdoptionBodyProps) {
  if (fixture) {
    return (
      <>
        <EmptyState text={ADOPTION_EMPTY} />
        <div className="chart-sub">Fixture playback has no server, so no practice events.</div>
      </>
    )
  }
  if (status === 'idle') return <EmptyState />
  if (status === 'unavailable' && days.length === 0) return <EmptyState text="practice events unavailable" />
  if (days.length === 0) return <EmptyState text={ADOPTION_EMPTY} />
  const t = adoptionTotals(days)
  const pct = (v: number | null) => (v === null ? '—' : fmtPct(v))
  return (
    <>
      <div className="sub">Practice events per day</div>
      <div className="chart-legend">
        {PRACTICE_KINDS.map((k) => (
          <span key={k} className="legend-item">
            <span className="legend-line" style={{ background: KIND_COLORS[k] }} />
            {k}
          </span>
        ))}
      </div>
      <AdoptionChart days={days} />
      <table style={TABLE}>
        <tbody>
          {PRACTICE_KINDS.map((k) => (
            <tr key={k} data-kind={k}>
              <td style={TD_L}>{k}</td>
              <td style={TD} className="tnum">
                {t[k]}
              </td>
            </tr>
          ))}
          <tr>
            <td style={TD_L} title="adopted / suggested">
              adoption rate
            </td>
            <td style={TD} className="tnum">
              {pct(t.adoptionRate)}
            </td>
          </tr>
          <tr>
            <td style={TD_L} title="persisted / adopted; a recent adoption has not had time to persist or drop">
              persistence
            </td>
            <td style={TD} className="tnum">
              {pct(t.persistence)}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="chart-sub" style={{ marginTop: 4 }}>
        Days are the days of each event&apos;s tick. Adoption is measured by the kernel, never self-reported.
        {truncated ? ` Showing the newest ${PRACTICES_LIMIT} events: the earliest days may be cut.` : ''}
        {status === 'unavailable' ? ' The last refresh failed; these are the last rows received.' : ''}
      </div>
    </>
  )
})

interface PracticesState {
  runId: string | null
  events: PracticeEventRow[] | null
  status: PracticesStatus
}

export const AdoptionCard = memo(function AdoptionCard({ fixture = FIXTURE_MODE }: { fixture?: boolean }) {
  const runId = useStore((s) => s.runId)
  const ticksPerDay = useStore((s) => s.config?.ticks_per_day ?? 24)
  const [state, setState] = useState<PracticesState>({ runId: null, events: null, status: 'idle' })
  useEffect(() => {
    if (runId === null || fixture) return
    return pollPractices(
      (events) => setState({ runId, events, status: 'ok' }),
      () => setState((prev) => (prev.runId === runId && prev.events ? { ...prev, status: 'unavailable' } : { runId, events: null, status: 'unavailable' })),
    )
  }, [runId, fixture])
  const mine = state.runId === runId ? state : null
  const days = useMemo(() => adoptionDays(mine?.events ?? [], ticksPerDay), [mine?.events, ticksPerDay])
  const status: PracticesStatus = mine?.status ?? 'idle'
  const t = days.length && !fixture ? adoptionTotals(days) : null
  const meta = t ? <span className="muted small tnum">{`${t.adopted}/${t.suggested} adopted`}</span> : undefined
  return (
    <Card id="adoption" title="Adoption" meta={meta}>
      <AdoptionBody days={days} status={status} truncated={(mine?.events?.length ?? 0) >= PRACTICES_LIMIT} fixture={fixture} />
    </Card>
  )
})
