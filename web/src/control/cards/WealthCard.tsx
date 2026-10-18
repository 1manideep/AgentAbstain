import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { TickRing } from '../../state/series'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { fmtUsd, SERIES } from '../charts/chartTheme'
import { Sparkline } from '../charts/Sparkline'
import { WealthChart, type WealthRow } from '../charts/WealthChart'

/** At most `max` evenly strided finite values of a tick ring, oldest first, always ending on the newest. */
function ringValues(ring: TickRing, max = 240): { values: number[]; first: number; last: number } {
  const n = ring.count
  const values: number[] = []
  if (n === 0) return { values, first: 0, last: 0 }
  const step = Math.max(1, Math.ceil(n / max))
  const take = (i: number) => {
    const v = ring.valueAt(i)
    if (Number.isFinite(v)) values.push(v)
  }
  for (let i = 0; i < n; i += step) take(i)
  if ((n - 1) % step !== 0) take(n - 1)
  return { values, first: ring.tickAt(0), last: ring.tickAt(n - 1) }
}

const fmtGini = (v: number) => v.toFixed(2)
const fmtBalance = (v: number) => fmtUsd(v)

/** Mean living balance and Gini over the tick window: two sparklines, one scale each (never a dual axis). */
const WealthTrend = memo(function WealthTrend({ balance, gini, first, last }: { balance: number[]; gini: number[]; first: number; last: number }) {
  if (balance.length === 0 && gini.length === 0) return null
  return (
    <div className="wealth-trend">
      <div className="chart-sub tnum">
        Mean balance of the living, ticks {first}-{last}
      </div>
      <Sparkline values={balance} color={SERIES[0]} height={40} format={fmtBalance} ariaLabel="mean balance over time" emptyText="No mean balance yet." />
      <div className="chart-sub">Gini of living balances</div>
      <Sparkline values={gini} color={SERIES[1]} height={40} format={fmtGini} ariaLabel="gini over time" emptyText="No gini yet." />
    </div>
  )
})

export const WealthCard = memo(function WealthCard() {
  const statsRev = useStore((s) => s.statsRev)
  const agentStats = useStore((s) => s.agentStats)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const series = useStore((s) => s.series)
  const seriesRev = useStore((s) => s.seriesRev)
  const { selectedId, select } = useStore(useShallow((s) => ({ selectedId: s.selectedAgentId, select: s.select })))
  const rows = useMemo(() => {
    void statsRev
    const out: WealthRow[] = []
    for (const st of agentStats.values()) {
      if (st.status !== 'alive') continue
      const a = rosterById.get(st.id)
      out.push({ id: st.id, name: a?.name ?? st.id, tier: a?.tier ?? '?', color: selectTierColor(config, a?.tier ?? ''), balance: st.balance, alive: true })
    }
    out.sort((x, y) => y.balance - x.balance)
    return out
  }, [statsRev, agentStats, rosterById, config])
  // legend: only tiers someone in the roster is on, ordered down the capability ladder, then by name
  const tiers = useMemo(() => {
    const used = new Set<string>()
    for (const a of rosterById.values()) used.add(a.tier)
    const rank = (name: string) => {
      const r = config?.tiers[name]?.rank
      return typeof r === 'number' ? r : Number.POSITIVE_INFINITY
    }
    return Object.keys(config?.tiers ?? {})
      .filter((name) => used.size === 0 || used.has(name))
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      .map((name) => ({ name, color: selectTierColor(config, name) }))
  }, [config, rosterById])
  // the tick rings, read at the store's commit rate (<= 4 Hz) like the other charts
  const trend = useMemo(() => {
    void seriesRev
    const balance = ringValues(series.meanBalanceByTick)
    const gini = ringValues(series.giniByTick)
    return { balance: balance.values, gini: gini.values, first: balance.first, last: balance.last }
  }, [seriesRev, series])
  return (
    <Card id="wealth" title="Wealth distribution" meta={<span className="muted small">{rows.length} alive</span>}>
      <WealthChart rows={rows} tiers={tiers} gini={series.gini} selectedId={selectedId} onSelect={select} />
      <WealthTrend balance={trend.balance} gini={trend.gini} first={trend.first} last={trend.last} />
    </Card>
  )
})
