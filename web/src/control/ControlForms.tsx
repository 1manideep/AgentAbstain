import { memo, useCallback, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { EpochBody, EpochKind } from '../protocol'
import { apiPost, controlsBlockedReason, describeResult, LIMITS } from '../net/api'
import { useStore } from '../state/store'

const KINDS: EpochKind[] = ['drought', 'storm', 'boom', 'arrival', 'custom']

/** The epoch body plus the optional start day the server accepts (void/server/api.py EpochBody.day). */
type EpochPost = EpochBody & { day?: number }

interface Feedback {
  text: string
  error: boolean
}

/** A finite number in [min, max] (min exclusive with `gt`, whole with `int`), or null; blank is null too. */
function inRange(raw: string, min: number, max: number | undefined, opts: { int?: boolean; gt?: boolean } = {}): number | null {
  if (raw.trim() === '') return null
  const n = Number(raw)
  if (!Number.isFinite(n)) return null
  if (opts.int && !Number.isInteger(n)) return null
  if (opts.gt ? n <= min : n < min) return null
  if (max !== undefined && n > max) return null
  return n
}

function useAction(): {
  busy: string | null
  msg: Feedback | null
  run: (key: string, path: string, body?: unknown) => Promise<boolean>
  fail: (text: string) => void
} {
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<Feedback | null>(null)
  const run = useCallback(async (key: string, path: string, body?: unknown) => {
    setBusy(key)
    setMsg(null)
    try {
      const result = await apiPost<unknown>(path, body)
      const byId = useStore.getState().rosterById
      setMsg({ text: describeResult(result, (id) => byId.get(id)?.name ?? id), error: false })
      return true
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), error: true })
      return false
    } finally {
      setBusy(null)
    }
  }, [])
  const fail = useCallback((text: string) => setMsg({ text, error: true }), [])
  return { busy, msg, run, fail }
}

/** Why the controls are off (run ended, or no hello yet), or null. */
function useBlocked(): string | null {
  return useStore((s) => controlsBlockedReason(s.runStatus))
}

function Note({ msg }: { msg: Feedback | null }) {
  if (!msg) return null
  return (
    <span className={msg.error ? 'error small' : 'muted small'} role={msg.error ? 'alert' : 'status'}>
      {msg.text}
    </span>
  )
}

const ServerControls = memo(function ServerControls() {
  const serverPaused = useStore((s) => s.serverPaused)
  const tickSeconds = useStore((s) => s.tickSeconds)
  const blocked = useBlocked()
  const { busy, msg, run, fail } = useAction()
  const [secs, setSecs] = useState(String(tickSeconds))
  const off = busy !== null || blocked !== null
  const setSpeed = useCallback(() => {
    const { min, max } = LIMITS.tickSeconds
    const v = inRange(secs, min, max)
    if (v === null) return fail(`tick seconds must be between ${min} and ${max}`)
    void run('speed', '/api/control/speed', { tick_seconds: v })
  }, [secs, run, fail])
  return (
    <div className="form">
      <div className="form-row">
        <span className="form-label">Server</span>
        {serverPaused ? (
          <button type="button" className="btn small" disabled={off} title={blocked ?? 'resume the tick loop now'} onClick={() => run('resume', '/api/control/resume')}>
            {busy === 'resume' ? '…' : 'resume'}
          </button>
        ) : (
          <button type="button" className="btn small" disabled={off} title={blocked ?? 'pause the tick loop now'} onClick={() => run('pause', '/api/control/pause')}>
            {busy === 'pause' ? '…' : 'pause'}
          </button>
        )}
        <button type="button" className="btn small" disabled={off || !serverPaused} title={blocked ?? (serverPaused ? 'run one tick and report it' : 'valid only while paused')} onClick={() => run('step', '/api/control/step')}>
          {busy === 'step' ? '…' : 'step'}
        </button>
        <input
          className="input narrow tnum"
          type="number"
          min={LIMITS.tickSeconds.min}
          max={LIMITS.tickSeconds.max}
          step="0.5"
          value={secs}
          onChange={(e) => setSecs(e.currentTarget.value)}
          aria-label="tick seconds"
          title={`now ${tickSeconds} s per tick`}
        />
        <button type="button" className="btn small" disabled={off} title={blocked ?? 'seconds between ticks, applied now'} onClick={setSpeed}>
          {busy === 'speed' ? '…' : 'set tick s'}
        </button>
      </div>
      {msg ? (
        <div>
          <Note msg={msg} />
        </div>
      ) : null}
    </div>
  )
})

const EpochForm = memo(function EpochForm() {
  // a fresh array per call would re-render forever (zustand compares snapshots by reference)
  const tiers = useStore(useShallow((s) => Object.keys(s.config?.tiers ?? {})))
  const blocked = useBlocked()
  const { busy, msg, run, fail } = useAction()
  const [kind, setKind] = useState<EpochKind>('drought')
  const [day, setDay] = useState('')
  const [days, setDays] = useState('1')
  const [scarcity, setScarcity] = useState('')
  const [weather, setWeather] = useState('')
  const [name, setName] = useState('Stranger')
  const [tier, setTier] = useState('')
  const [balance, setBalance] = useState('1.5')
  const submit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      const L = LIMITS
      const problems: string[] = []
      const duration = inRange(days, L.durationDays.min, L.durationDays.max, { int: true })
      if (duration === null) problems.push(`days must be a whole number from ${L.durationDays.min} to ${L.durationDays.max}`)
      const body: EpochPost = { kind, duration_days: duration ?? 1 }
      if (day.trim() !== '') {
        const d = inRange(day, L.epochDay.min, undefined, { int: true })
        if (d === null) problems.push(`start day must be a whole number from ${L.epochDay.min}`)
        else body.day = d
      }
      if (scarcity.trim() !== '') {
        const v = inRange(scarcity, L.scarcity.min, L.scarcity.max)
        if (v === null) problems.push(`scarcity must be between ${L.scarcity.min} and ${L.scarcity.max}`)
        else body.scarcity = v
      }
      if (weather.trim() !== '') {
        const v = inRange(weather, L.weather.min, L.weather.max)
        if (v === null) problems.push(`weather must be between ${L.weather.min} and ${L.weather.max}`)
        else body.weather_baseline = v
      }
      if (kind === 'arrival') {
        const b = inRange(balance, L.arrivalBalance.min, L.arrivalBalance.max)
        if (b === null) problems.push(`arrival balance must be between ${L.arrivalBalance.min} and ${L.arrivalBalance.max} USD`)
        body.arrival = { name: name.trim() || 'Stranger', tier: tier || tiers[0] || '', balance_usd: b ?? 0 }
      }
      if (problems.length) return fail(problems.join('; '))
      void run('epoch', '/api/control/epoch', body)
    },
    [kind, day, days, scarcity, weather, name, tier, balance, tiers, run, fail],
  )
  const L = LIMITS
  return (
    <form className="form" onSubmit={submit} noValidate>
      <div className="form-row">
        <span className="form-label">Epoch</span>
        <select className="select" value={kind} onChange={(e) => setKind(e.currentTarget.value as EpochKind)} aria-label="epoch kind">
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input className="input narrow tnum" type="number" min={L.epochDay.min} step="1" placeholder="day: now" value={day} onChange={(e) => setDay(e.currentTarget.value)} aria-label="start day" title="start day (blank: the current day, from the next tick)" />
        <input className="input narrow tnum" type="number" min={L.durationDays.min} max={L.durationDays.max} step="1" value={days} onChange={(e) => setDays(e.currentTarget.value)} aria-label="duration days" title={`duration in days (${L.durationDays.min}-${L.durationDays.max})`} />
        <input className="input narrow tnum" type="number" step="0.1" min={L.scarcity.min} max={L.scarcity.max} placeholder="scarcity" value={scarcity} onChange={(e) => setScarcity(e.currentTarget.value)} aria-label="scarcity" title={`scarcity (${L.scarcity.min}-${L.scarcity.max})`} />
        <input className="input narrow tnum" type="number" step="0.1" min={L.weather.min} max={L.weather.max} placeholder="weather" value={weather} onChange={(e) => setWeather(e.currentTarget.value)} aria-label="weather baseline" title={`weather baseline (${L.weather.min} to ${L.weather.max})`} />
      </div>
      {kind === 'arrival' ? (
        <div className="form-row">
          <input className="input" placeholder="name" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={L.arrivalName} aria-label="arrival name" />
          <select className="select" value={tier} onChange={(e) => setTier(e.currentTarget.value)} aria-label="tier">
            {tiers.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input className="input narrow tnum" type="number" step="0.05" min={L.arrivalBalance.min} max={L.arrivalBalance.max} value={balance} onChange={(e) => setBalance(e.currentTarget.value)} aria-label="balance usd" title={`starting balance, USD (${L.arrivalBalance.min}-${L.arrivalBalance.max})`} />
        </div>
      ) : null}
      <div className="form-row">
        <button type="submit" className="btn small" disabled={busy !== null || blocked !== null} title={blocked ?? undefined}>
          {busy === 'epoch' ? '…' : 'apply epoch'}
        </button>
        <Note msg={msg} />
      </div>
    </form>
  )
})

const BenefactorForm = memo(function BenefactorForm() {
  const roster = useStore((s) => s.roster)
  const blocked = useBlocked()
  const { busy, msg, run, fail } = useAction()
  const [agent, setAgent] = useState('')
  const [amount, setAmount] = useState('0.50')
  const { min, max } = LIMITS.benefactorAmount
  const submit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault()
      const a = inRange(amount, min, max, { gt: true })
      if (a === null) return fail(`amount must be more than ${min} and at most ${max} USD`)
      void run('grant', '/api/control/benefactor', agent ? { agent_id: agent, amount_usd: a } : { amount_usd: a })
    },
    [agent, amount, min, max, run, fail],
  )
  const off = busy !== null || blocked !== null
  return (
    <form className="form" onSubmit={submit} noValidate>
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
        <input className="input narrow tnum" type="number" step="0.05" min="0.01" max={max} value={amount} onChange={(e) => setAmount(e.currentTarget.value)} aria-label="amount usd" title={`grant, USD (more than ${min}, at most ${max})`} />
        <button type="submit" className="btn small" disabled={off} title={blocked ?? undefined}>
          {busy === 'grant' ? '…' : 'grant'}
        </button>
      </div>
      <div className="form-row">
        <button type="button" className="btn tiny" disabled={off} title={blocked ?? undefined} onClick={() => run('on', '/api/control/benefactor', { enabled: true })}>
          enable scheduled grants
        </button>
        <button type="button" className="btn tiny" disabled={off} title={blocked ?? undefined} onClick={() => run('off', '/api/control/benefactor', { enabled: false })}>
          disable
        </button>
        <Note msg={msg} />
      </div>
    </form>
  )
})

export const ControlForms = memo(function ControlForms() {
  const blocked = useBlocked()
  return (
    <div className="controls">
      {blocked ? (
        <div className="bad-text small" role="status">
          Controls are off: {blocked}.
        </div>
      ) : null}
      <ServerControls />
      <EpochForm />
      <BenefactorForm />
    </div>
  )
})
