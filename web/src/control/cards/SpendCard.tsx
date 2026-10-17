import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { SpendGauges } from '../charts/SpendGauges'

export const SpendCard = memo(function SpendCard() {
  const { spendToday, spendTotal, spawnPool } = useStore(useShallow((s) => ({ spendToday: s.world.spendToday, spendTotal: s.world.spendTotal, spawnPool: s.world.spawnPool })))
  const { dailyCap, totalCap } = useStore(useShallow((s) => ({ dailyCap: s.config?.daily_cap_usd ?? 0, totalCap: s.config?.total_cap_usd ?? 0 })))
  const poolMax = useMemo(() => Math.max(spawnPool, 5), [spawnPool])
  return (
    <Card id="spend" title="Spend" meta={<span className="muted small tnum">caps {dailyCap.toFixed(2)} / {totalCap.toFixed(2)}</span>}>
      <SpendGauges spendToday={spendToday} spendTotal={spendTotal} spawnPool={spawnPool} dailyCap={dailyCap} totalCap={totalCap} spawnPoolMax={poolMax} />
    </Card>
  )
})
