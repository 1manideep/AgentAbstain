import { memo, useCallback, useState } from 'react'
import type { EpochBody, EpochKind } from '../protocol'
import { apiPost, FIXTURE_MODE } from '../net/api'
import { useStore } from '../state/store'

const KINDS: EpochKind[] = ['drought', 'storm', 'boom', 'arrival', 'custom']

function useAction(): { busy: string | null; msg: string | null; run: (key: string, path: string, body?: unknown) => Promise<void> } {
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const run = useCallback(async (key: string, path: string, body?: unknown) => {
    setBusy(key)
    setMsg(null)
    try {
      const ack = await apiPost<{ cmd_id?: string; will_apply_at_tick?: number }>(path, body)
      setMsg(FIXTURE_MODE ? 'fixture mode: logged, not sent' : ack.will_apply_at_tick !== undefined ? `queued for tick ${ack.will_apply_at_tick}` : 'queued')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }, [])
  return { busy, msg, run }
}

const ServerControls = memo(function ServerControls() {
  const serverPaused = useStore((s) => s.serverPaused)
  const tickSeconds = useStore((s) => s.tickSeconds)
  const { busy, msg, run } = useAction()
  const [secs, setSecs] = useState(String(tickSeconds))
  return (
    <div className="form">
      <div className="form-row">
        <span className="form-label">Server</span>
        {serverPaused ? (
          <button type="button" className="btn small" disabled={busy !== null} onClick={() => run('resume', '/api/control/resume')}>
            resume
          </button>
        ) : (
          <button type="button" className="btn small" disabled={busy !== null} onClick={() => run('pause', '/api/control/pause')}>
            pause
          </button>
        )}
        <button type="button" className="btn small" disabled={busy !== null || !serverPaused} title="valid only while paused" onClick={() => run('step', '/api/control/step')}>
          step
        </button>
        <input className="input narrow tnum" type="number" min="0" step="0.5" value={secs} onChange={(e) => setSecs(e.currentTarget.value)} aria-label="tick seconds" />
        <button type="button" className="btn small" disabled={busy !== null} onClick={() => run('speed', '/api/control/speed', { tick_seconds: Number(secs) })}>
          set tick s
        </button>
      </div>
      {msg ? <div className="muted small">{msg}</div> : null}
    </div>
  )
})

const EpochForm = memo(function EpochForm() {
  const tiers = useStore((s) => Object.keys(s.config?.tiers ?? {}))
  const { busy, msg, run } = useAction()
  const [kind, setKind] = useState<EpochKind>('drought')
  const [days, setDays] = useState('1')
  const [scarcity, setScarcity] = useState('')
  const [weather, setWeather] = useState('')
  const [name, setName] = useState('Stranger')
  const [tier, setTier] = useState('')
  const [balance, setBalance] = useState('1.5')
  const submit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      const body: EpochBody = { kind, duration_days: Math.max(1, Math.floor(Number(days) || 1)) }
      if (scarcity !== '' && Number.isFinite(Number(scarcity))) body.scarcity = Number(scarcity)
      if (weather !== '' && Number.isFinite(Number(weather))) body.weather_baseline = Number(weather)
      if (kind === 'arrival') body.arrival = { name: name.trim() || 'Stranger', tier: tier || tiers[0] || '', balance_usd: Number(balance) || 1 }
      void run('epoch', '/api/control/epoch', body)
    },
    [kind, days, scarcity, weather, name, tier, balance, tiers, run],
  )
  return (
    <form className="form" onSubmit={submit}>
      <div className="form-row">
        <span className="form-label">Epoch</span>
        <select className="select" value={kind} onChange={(e) => setKind(e.currentTarget.value as EpochKind)} aria-label="epoch kind">
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input className="input narrow tnum" type="number" min="1" step="1" value={days} onChange={(e) => setDays(e.currentTarget.value)} aria-label="duration days" title="duration (days)" />
        <input className="input narrow tnum" type="number" step="0.1" placeholder="scarcity" value={scarcity} onChange={(e) => setScarcity(e.currentTarget.value)} aria-label="scarcity" />
        <input className="input narrow tnum" type="number" step="0.1" min="-1" max="1" placeholder="weather" value={weather} onChange={(e) => setWeather(e.currentTarget.value)} aria-label="weather baseline" />
      </div>
      {kind === 'arrival' ? (
        <div className="form-row">
          <input className="input" placeholder="name" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={24} />
          <select className="select" value={tier} onChange={(e) => setTier(e.currentTarget.value)} aria-label="tier">
            {tiers.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input className="input narrow tnum" type="number" step="0.05" min="0" value={balance} onChange={(e) => setBalance(e.currentTarget.value)} aria-label="balance usd" />
        </div>
      ) : null}
      <div className="form-row">
        <button type="submit" className="btn small" disabled={busy !== null}>
          {busy === 'epoch' ? '…' : 'apply epoch'}
        </button>
        {msg ? <span className="muted small">{msg}</span> : null}
      </div>
    </form>
  )
})

const BenefactorForm = memo(function BenefactorForm() {
  const roster = useStore((s) => s.roster)
  const { busy, msg, run } = useAction()
  const [agent, setAgent] = useState('')
  const [amount, setAmount] = useState('0.50')
  const submit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      const a = Number(amount)
      if (!Number.isFinite(a) || a <= 0) return
      void run('grant', '/api/control/benefactor', agent ? { agent_id: agent, amount_usd: a } : { amount_usd: a })
    },
    [agent, amount, run],
  )
  return (
    <form className="form" onSubmit={submit}>
      <div className="form-row">
        <span className="form-label">Benefactor</span>
        <select className="select" value={agent} onChange={(e) => setAgent(e.currentTarget.value)} aria-label="target agent">
          <option value="">random target</option>
          {roster
            .filter((a) => a.status === 'alive')
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
        </select>
        <input className="input narrow tnum" type="number" step="0.05" min="0.01" value={amount} onChange={(e) => setAmount(e.currentTarget.value)} aria-label="amount usd" />
        <button type="submit" className="btn small" disabled={busy !== null}>
          {busy === 'grant' ? '…' : 'grant'}
        </button>
      </div>
      <div className="form-row">
        <button type="button" className="btn tiny" disabled={busy !== null} onClick={() => run('on', '/api/control/benefactor', { enabled: true })}>
          enable scheduled grants
        </button>
        <button type="button" className="btn tiny" disabled={busy !== null} onClick={() => run('off', '/api/control/benefactor', { enabled: false })}>
          disable
        </button>
        {msg ? <span className="muted small">{msg}</span> : null}
      </div>
    </form>
  )
})

export const ControlForms = memo(function ControlForms() {
  return (
    <div className="controls">
      <ServerControls />
      <EpochForm />
      <BenefactorForm />
    </div>
  )
})
