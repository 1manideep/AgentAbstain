import { memo, useCallback, useState } from 'react'
import type { TaskItem } from '../protocol'
import { apiPost, controlsBlockedReason, describeResult, LIMITS } from '../net/api'
import { useStore } from '../state/store'
import { fmtUsd } from './charts/chartTheme'

interface Feedback {
  text: string
  error: boolean
}

function usePost(): { busy: string | null; msg: Feedback | null; post: (key: string, path: string, body?: unknown) => Promise<boolean>; fail: (text: string) => void } {
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<Feedback | null>(null)
  const post = useCallback(async (key: string, path: string, body?: unknown) => {
    setBusy(key)
    setMsg(null)
    try {
      setMsg({ text: describeResult(await apiPost<unknown>(path, body)), error: false })
      return true
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), error: true })
      return false
    } finally {
      setBusy(null)
    }
  }, [])
  const fail = useCallback((text: string) => setMsg({ text, error: true }), [])
  return { busy, msg, post, fail }
}

function Note({ msg, block }: { msg: Feedback | null; block?: boolean }) {
  if (!msg) return null
  const cls = (msg.error ? 'error' : 'muted') + ' small'
  return block ? (
    <div className={cls} role={msg.error ? 'alert' : 'status'}>
      {msg.text}
    </div>
  ) : (
    <span className={cls} role={msg.error ? 'alert' : 'status'}>
      {msg.text}
    </span>
  )
}

const Task = memo(function Task({ t, blocked }: { t: TaskItem; blocked: string | null }) {
  const rosterById = useStore((s) => s.rosterById)
  const { busy, msg, post } = usePost()
  const name = (id: string | null) => (id ? (rosterById.get(id)?.name ?? id) : '—')
  const off = busy !== null || blocked !== null
  return (
    <div className={'task status-' + t.status}>
      <div className="task-head">
        <span className="task-title" title={t.title}>
          {t.title}
        </span>
        <span className="task-reward tnum">{fmtUsd(t.reward_usd, 2)}</span>
        <span className={'tag ' + t.status}>{t.status}</span>
      </div>
      {t.description ? <div className="pitch small">{t.description}</div> : null}
      <div className="muted small tnum">
        posted tick {t.posted_tick}
        {t.assigned_agent_id ? ` · assigned to ${name(t.assigned_agent_id)}` : ''}
        {typeof t.completed_tick === 'number' ? ` · completed tick ${t.completed_tick}` : ''}
      </div>
      {t.applications.length > 0 ? (
        <ul className="applications">
          {t.applications.map((a) => (
            <li key={a.id} className={'application ' + a.status}>
              <div className="application-head">
                <b>{name(a.agent_id)}</b>
                <span className="muted small tnum">
                  fee {fmtUsd(a.fee_usd)} · tick {a.tick}
                </span>
                <span className={'tag ' + a.status}>{a.status}</span>
                {t.status === 'open' && a.status === 'pending' ? (
                  <button
                    type="button"
                    className="btn tiny"
                    disabled={off}
                    title={blocked ?? undefined}
                    onClick={() => post(a.id, `/api/tasks/${encodeURIComponent(t.id)}/approve/${encodeURIComponent(a.id)}`)}
                  >
                    {busy === a.id ? '…' : 'approve'}
                  </button>
                ) : null}
              </div>
              <div className="pitch">{a.pitch}</div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="muted small">no applications</div>
      )}
      {t.status === 'assigned' ? (
        <button type="button" className="btn small" disabled={off} title={blocked ?? undefined} onClick={() => post('complete', `/api/tasks/${encodeURIComponent(t.id)}/complete`)}>
          {busy === 'complete' ? '…' : 'mark complete'}
        </button>
      ) : null}
      <Note msg={msg} block />
    </div>
  )
})

export const TaskBoard = memo(function TaskBoard() {
  const tasks = useStore((s) => s.tasks)
  const blocked = useStore((s) => controlsBlockedReason(s.runStatus))
  const { busy, msg, post, fail } = usePost()
  const [title, setTitle] = useState('')
  const [desc, setDesc] = useState('')
  const [reward, setReward] = useState('0.25')
  const { min, max } = LIMITS.taskReward
  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      const r = reward.trim() === '' ? Number.NaN : Number(reward)
      if (!title.trim()) return fail('a task needs a title')
      if (!Number.isFinite(r) || r < min || r > max) return fail(`reward must be between ${min} and ${max} USD`)
      // inputs are cleared only once the server has accepted the task
      if (await post('new', '/api/tasks', { title: title.trim(), description: desc.trim(), reward_usd: r })) {
        setTitle('')
        setDesc('')
      }
    },
    [post, fail, title, desc, reward, min, max],
  )
  const items = tasks?.items ?? []
  return (
    <div className="taskboard">
      {items.length === 0 ? <div className="muted small">No tasks posted.</div> : items.map((t) => <Task key={t.id} t={t} blocked={blocked} />)}
      <form className="form" onSubmit={submit} noValidate>
        {blocked ? <div className="bad-text small">Posting is off: {blocked}.</div> : null}
        <div className="form-row">
          <input className="input" placeholder="new task title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} maxLength={LIMITS.taskTitle} aria-label="task title" />
          <span className="muted small tnum">
            {title.length}/{LIMITS.taskTitle}
          </span>
          <input className="input narrow tnum" type="number" step="0.01" min={min} max={max} value={reward} onChange={(e) => setReward(e.currentTarget.value)} aria-label="reward usd" title={`reward, USD (${min}-${max})`} />
        </div>
        <input className="input" placeholder="description" value={desc} onChange={(e) => setDesc(e.currentTarget.value)} maxLength={LIMITS.taskDescription} aria-label="task description" />
        <div className="form-row">
          <button type="submit" className="btn small" disabled={busy !== null || blocked !== null || !title.trim()} title={blocked ?? undefined}>
            {busy === 'new' ? '…' : 'post task'}
          </button>
          <Note msg={msg} />
        </div>
      </form>
    </div>
  )
})
