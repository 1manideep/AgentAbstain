import { memo } from 'react'
import { fmtUsd, SERIES, STATUS } from './chartTheme'

interface GaugeProps {
  label: string
  value: number
  max: number
  /** When true the fill turns to warning/critical as it approaches max. */
  capped: boolean
}

function severity(ratio: number): { color: string; icon: string; word: string } {
  if (ratio >= 0.95) return { color: STATUS.critical, icon: '●', word: 'at cap' }
  if (ratio >= 0.8) return { color: STATUS.serious, icon: '▲', word: 'near cap' }
  if (ratio >= 0.6) return { color: STATUS.warning, icon: '▲', word: 'rising' }
  return { color: SERIES[0], icon: '', word: '' }
}

const Gauge = memo(function Gauge({ label, value, max, capped }: GaugeProps) {
  const ratio = max > 0 ? Math.min(1, value / max) : 0
  const sev = capped ? severity(ratio) : { color: SERIES[0], icon: '', word: '' }
  return (
    <div className="gauge">
      <div className="gauge-head">
        <span className="gauge-label">{label}</span>
        <span className="gauge-value tnum">
          {fmtUsd(value)}
          {capped ? <span className="muted"> / {fmtUsd(max, 2)}</span> : null}
        </span>
      </div>
      <div className="gauge-track" role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label={label}>
        <div className="gauge-fill" style={{ width: `${ratio * 100}%`, background: sev.color }} />
      </div>
      {sev.word ? (
        <div className="gauge-status" style={{ color: sev.color }}>
          <span aria-hidden="true">{sev.icon}</span> {sev.word}
        </div>
      ) : null}
    </div>
  )
})

interface SpendGaugesProps {
  spendToday: number
  spendTotal: number
  spawnPool: number
  dailyCap: number
  totalCap: number
  spawnPoolMax: number
}

export const SpendGauges = memo(function SpendGauges(p: SpendGaugesProps) {
  return (
    <div className="gauges">
      <Gauge label="Spend today" value={p.spendToday} max={p.dailyCap} capped />
      <Gauge label="Spend total" value={p.spendTotal} max={p.totalCap} capped />
      <Gauge label="Spawn pool" value={p.spawnPool} max={Math.max(p.spawnPoolMax, 0.01)} capped={false} />
    </div>
  )
})
