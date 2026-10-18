import { memo, useEffect, useState } from 'react'
import type { ChronicleResponse } from '../protocol'
import { ApiError, apiGet, FIXTURE_MODE } from '../net/api'
import { useStore } from '../state/store'

/**
 * The chronicle (DESIGN §15 "GET /api/chronicle?day="): the latest issue from the stream, and back issues paged
 * through one day at a time. Headline and body are plain text: the chronicle's markdown is never rendered as HTML.
 */

/** The loop writes day n's paper at the start of day n + 1, and days count from 1 (void/sim/loop.py _begin_day). */
export const FIRST_DAY = 1

/** The pager: `day` null follows the latest issue as new ones arrive; a number holds that back issue. */
export interface PagerState {
  day: number | null
}

export type PagerAction = { type: 'older' } | { type: 'newer' } | { type: 'latest' } | { type: 'go'; day: number }

export interface PagerView {
  /** The issue on screen, or null before the first one. */
  shown: number | null
  following: boolean
  canOlder: boolean
  canNewer: boolean
}

// oxlint-disable-next-line react/only-export-components
export const FOLLOW: PagerState = { day: null }

/** A held day at or past the latest issue is the latest issue: the pager follows again. */
function hold(day: number, latest: number): PagerState {
  if (day >= latest) return FOLLOW
  return { day: Math.max(FIRST_DAY, Math.floor(day)) }
}

/** The next pager state; `latest` is the newest written day (null before the first issue). */
// oxlint-disable-next-line react/only-export-components
export function pagerReducer(state: PagerState, action: PagerAction, latest: number | null): PagerState {
  if (latest === null) return FOLLOW
  const shown = state.day === null ? latest : Math.min(state.day, latest)
  switch (action.type) {
    case 'older':
      return shown > FIRST_DAY ? hold(shown - 1, latest) : state.day === null ? FOLLOW : hold(shown, latest)
    case 'newer':
      return state.day === null ? FOLLOW : hold(shown + 1, latest)
    case 'latest':
      return FOLLOW
    case 'go':
      return Number.isFinite(action.day) ? hold(action.day, latest) : state
  }
}

/** What the pager shows and which way it can move. */
// oxlint-disable-next-line react/only-export-components
export function pagerView(state: PagerState, latest: number | null): PagerView {
  if (latest === null) return { shown: null, following: true, canOlder: false, canNewer: false }
  const held = state.day !== null && state.day < latest
  const shown = held ? Math.max(FIRST_DAY, state.day!) : latest
  return { shown, following: !held, canOlder: shown > FIRST_DAY, canNewer: held }
}

// ------------------------------------------------------------------ view

export interface Issue {
  day: number
  headline: string
  markdown: string
  rev?: number
}

export const ChronicleIssue = memo(function ChronicleIssue({ issue, back }: { issue: Issue; back: boolean }) {
  return (
    <article className="chronicle" data-day={issue.day}>
      <h3 className="chronicle-headline">{issue.headline || `Day ${issue.day}`}</h3>
      <div className="muted small">
        day {issue.day}
        {back ? ' · back issue' : issue.rev !== undefined ? ` · rev ${issue.rev}` : ''}
      </div>
      <pre className="chronicle-text">{issue.markdown}</pre>
    </article>
  )
})

export interface PagerBarProps {
  view: PagerView
  latest: number | null
  onAction: (a: PagerAction) => void
  /** Fixture playback has no server behind it, so there are no back issues to page through. */
  fixture: boolean
}

export const PagerBar = memo(function PagerBar({ view, latest, onAction, fixture }: PagerBarProps) {
  if (latest === null) return null
  const off = fixture
  return (
    <div className="chronicle-pager" style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '6px 0 0' }}>
      <button type="button" className="btn tiny" disabled={off || !view.canOlder} onClick={() => onAction({ type: 'older' })} aria-label="older issue">
        older
      </button>
      <span className="muted small tnum" aria-live="polite">
        {view.shown === null ? '' : `day ${view.shown} of ${latest}`}
      </span>
      <button type="button" className="btn tiny" disabled={off || !view.canNewer} onClick={() => onAction({ type: 'newer' })} aria-label="newer issue">
        newer
      </button>
      <button type="button" className="btn tiny" disabled={off || view.following} onClick={() => onAction({ type: 'latest' })}>
        latest
      </button>
    </div>
  )
})

export type BackIssue =
  | { status: 'loading' }
  | { status: 'ok'; issue: Issue }
  | { status: 'missing' }
  | { status: 'error'; message: string }

/** The body under the pager for a back issue. */
export const BackIssueBody = memo(function BackIssueBody({ day, state }: { day: number; state: BackIssue }) {
  if (state.status === 'ok') return <ChronicleIssue issue={state.issue} back />
  if (state.status === 'missing') return <div className="muted small card-empty">{`No issue was written for day ${day}.`}</div>
  if (state.status === 'error') return <div className="error small">{state.message}</div>
  return <div className="muted small">{`Loading day ${day}...`}</div>
})

export const NO_ISSUE = 'No chronicle yet: the first day has not been written.'
export const FIXTURE_NOTE = 'Back issues are served by the server; fixture playback has only the latest issue.'

export interface ChronicleBodyProps {
  /** The latest issue from the stream, or null before the first one. */
  latest: Issue | null
  view: PagerView
  /** The back issue on screen; ignored while the pager follows the latest. */
  back: BackIssue
  onAction: (a: PagerAction) => void
  fixture: boolean
}

/** The card's body from props (the component itself reads the store and fetches back issues). */
export const ChronicleBody = memo(function ChronicleBody({ latest, view, back, onAction, fixture }: ChronicleBodyProps) {
  if (latest === null) return <div className="muted small">{NO_ISSUE}</div>
  const backDay = view.following ? null : view.shown
  return (
    <>
      <PagerBar view={view} latest={latest.day} onAction={onAction} fixture={fixture} />
      {fixture ? <div className="chart-sub">{FIXTURE_NOTE}</div> : null}
      {backDay === null ? <ChronicleIssue issue={latest} back={false} /> : <BackIssueBody day={backDay} state={back} />}
    </>
  )
})

/** Answered back issues of one run by day: written papers and days without one. A past day's paper does not change. */
interface Answered {
  runId: string | null
  byDay: ReadonlyMap<number, BackIssue>
}

const NONE: ReadonlyMap<number, BackIssue> = new Map()

export const Chronicle = memo(function Chronicle({ fixture = FIXTURE_MODE }: { fixture?: boolean }) {
  const c = useStore((s) => s.chronicle)
  const runId = useStore((s) => s.runId)
  const [pager, setPager] = useState<{ runId: string | null; state: PagerState }>({ runId: null, state: FOLLOW })
  const [answered, setAnswered] = useState<Answered>({ runId: null, byDay: NONE })
  /** The last failed fetch; any pager move clears it, so coming back to that day asks again. */
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null)

  const latest = c && typeof c.day === 'number' && (c.headline || c.markdown) ? c.day : null
  // a new run starts on its latest issue
  const state = pager.runId === runId && !fixture ? pager.state : FOLLOW
  const view = pagerView(state, latest)
  const backDay = view.following ? null : view.shown
  const key = backDay === null ? null : `${runId ?? ''}:${backDay}`
  const cached = backDay === null ? undefined : (answered.runId === runId ? answered.byDay : NONE).get(backDay)
  const error = failed !== null && failed.key === key ? failed.message : null
  const fetchDay = backDay !== null && !fixture && cached === undefined && error === null ? backDay : null

  useEffect(() => {
    if (fetchDay === null) return
    let cancelled = false
    const put = (s: BackIssue) =>
      setAnswered((prev) => {
        const byDay = new Map(prev.runId === runId ? prev.byDay : NONE)
        byDay.set(fetchDay, s)
        return { runId, byDay }
      })
    apiGet<ChronicleResponse>(`/api/chronicle?day=${fetchDay}`)
      .then((r) => {
        if (cancelled) return
        put({
          status: 'ok',
          issue: {
            day: typeof r?.day === 'number' ? r.day : fetchDay,
            headline: typeof r?.headline === 'string' ? r.headline : '',
            markdown: typeof r?.markdown === 'string' ? r.markdown : '',
          },
        })
      })
      .catch((e: unknown) => {
        if (cancelled) return
        if (e instanceof ApiError && e.status === 404) put({ status: 'missing' })
        else setFailed({ key: `${runId ?? ''}:${fetchDay}`, message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [fetchDay, runId])

  const onAction = (a: PagerAction) => {
    setFailed(null)
    setPager({ runId, state: pagerReducer(state, a, latest) })
  }

  const back: BackIssue = cached ?? (error !== null ? { status: 'error', message: error } : { status: 'loading' })
  const issue = c && latest !== null ? { day: latest, headline: c.headline, markdown: c.markdown, rev: c.rev } : null
  return <ChronicleBody latest={issue} view={view} back={back} onAction={onAction} fixture={fixture} />
})
