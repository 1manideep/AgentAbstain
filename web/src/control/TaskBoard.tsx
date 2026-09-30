import { memo, useCallback, useState } from 'react'
import type { TaskItem } from '../protocol'
import { apiPost } from '../net/api'
import { useStore } from '../state/store'
import { fmtUsd } from './charts/chartTheme'

function usePost(): { busy: string | null; err: string | null; post: (key: string, path: string, body?: unknown) => Promise<void> } {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const post = useCallback(async (key: string, path: string, body?: unknown) => {
    setBusy(key)
    setErr(null)
    try {
      await apiPost(path, body)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }, [])
  return { busy, err, post }
}

const Task = memo(function Task({ t }: { t: TaskItem }) {
  const rosterById = useStore((s) => s.rosterById)
  const { busy, err, post } = usePost()
  const name = (id: string | null) => (id ? (rosterById.get(id)?.name ?? id) : '—')
  return (
    <div className={'task status-' + t.status}>
      <div className="task-head">
        <span className="task-title">{t.title}</span>
        <span className="task-reward tnum">{fmtUsd(t.reward_usd, 2)}</span>
        <span className={'tag ' + t.status}>{t.status}</span>
      </div>
      <div className="muted small">
        posted tick {t.posted_tick}
        {t.assigned_agent_id ? ` · assigned to ${name(t.assigned_agent_id)}` : ''}
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
                  <button type="button" className="btn tiny" disabled={busy !== null} onClick={() => post(a.id, `/api/tasks/${encodeURIComponent(t.id)}/approve/${encodeURIComponent(a.id)}`)}>
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
        <button type="button" className="btn small" disabled={busy !== null} onClick={() => post('complete', `/api/tasks/${encodeURIComponent(t.id)}/complete`)}>
          {busy === 'complete' ? '…' : 'mark complete'}
        </button>
      ) : null}
      {err ? <div className="error small">{err}</div> : null}
    </div>
  )
})

export const TaskBoard = memo(function TaskBoard() {
  const tasks = useStore((s) => s.tasks)
  const { busy, err, post } = usePost()
  const [title, setTitle] = useState('')
  const [desc, setDesc] = useState('')
  const [reward, setReward] = useState('0.25')
  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      const r = Number(reward)
      if (!title.trim() || !Number.isFinite(r) || r <= 0) return
      await post('new', '/api/tasks', { title: title.trim(), description: desc.trim(), reward_usd: r })
      setTitle('')
      setDesc('')
    },
    [post, title, desc, reward],
  )
  const items = tasks?.items ?? []
  return (
    <div className="taskboard">
      {items.length === 0 ? <div className="muted small">No tasks posted.</div> : items.map((t) => <Task key={t.id} t={t} />)}
      <form className="form" onSubmit={submit}>
        <div className="form-row">
          <input className="input" placeholder="new task title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} maxLength={120} />
          <input className="input narrow tnum" type="number" step="0.01" min="0.01" value={reward} onChange={(e) => setReward(e.currentTarget.value)} aria-label="reward usd" />
        </div>
        <input className="input" placeholder="description" value={desc} onChange={(e) => setDesc(e.currentTarget.value)} maxLength={600} />
        <div className="form-row">
          <button type="submit" className="btn small" disabled={busy !== null || !title.trim()}>
            {busy === 'new' ? '…' : 'post task'}
          </button>
          {err ? <span className="error small">{err}</span> : null}
        </div>
      </form>
    </div>
  )
})
