import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { WealthChart, type WealthRow } from '../charts/WealthChart'

export const WealthCard = memo(function WealthCard() {
  const statsRev = useStore((s) => s.statsRev)
  const agentStats = useStore((s) => s.agentStats)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const series = useStore((s) => s.series)
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
  return (
    <Card id="wealth" title="Wealth distribution" meta={<span className="muted small">{rows.length} alive</span>}>
      <WealthChart rows={rows} tiers={tiers} gini={series.gini} selectedId={selectedId} onSelect={select} />
    </Card>
  )
})
