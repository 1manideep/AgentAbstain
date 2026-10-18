import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { fmtUsd, SERIES } from '../charts/chartTheme'
import { SpendGauges } from '../charts/SpendGauges'

/** The house kernel wallet (DESIGN §9.1), drawn like the spawn-pool gauge it sits under and on the same scale. */
const HouseGauge = memo(function HouseGauge({ value, max }: { value: number; max: number }) {
  const ratio = max > 0 && Number.isFinite(value) ? Math.min(1, Math.max(0, value / max)) : 0
  return (
    <div className="gauge house-gauge" style={{ marginTop: 10 }}>
      <div className="gauge-head">
        <span className="gauge-label">House wallet</span>
        <span className="gauge-value tnum">{fmtUsd(value)}</span>
      </div>
      <div className="gauge-track" role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label="House wallet">
        <div className="gauge-fill" style={{ width: `${ratio * 100}%`, background: SERIES[0] }} />
      </div>
      <div className="gauge-status muted">absorbs overruns, collects fees</div>
    </div>
  )
})

export const SpendCard = memo(function SpendCard() {
  const { spendToday, spendTotal, spawnPool, house } = useStore(
    useShallow((s) => ({ spendToday: s.world.spendToday, spendTotal: s.world.spendTotal, spawnPool: s.world.spawnPool, house: s.world.houseUsd })),
  )
  const { dailyCap, totalCap } = useStore(useShallow((s) => ({ dailyCap: s.config?.daily_cap_usd ?? 0, totalCap: s.config?.total_cap_usd ?? 0 })))
  // the two kernel wallets share one scale so their bars compare
  const kernelMax = useMemo(() => Math.max(spawnPool, house, 5), [spawnPool, house])
  return (
    <Card id="spend" title="Spend" meta={<span className="muted small tnum">caps {dailyCap.toFixed(2)} / {totalCap.toFixed(2)}</span>}>
      <SpendGauges spendToday={spendToday} spendTotal={spendTotal} spawnPool={spawnPool} dailyCap={dailyCap} totalCap={totalCap} spawnPoolMax={kernelMax} />
      <HouseGauge value={house} max={kernelMax} />
    </Card>
  )
})
