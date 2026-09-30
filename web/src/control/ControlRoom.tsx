import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { MAX_GENERATIONS, STRESS_BINS } from '../state/series'
import { selectTierColor, useStore } from '../state/store'
import { AgentPanel } from './AgentPanel'
import { Card } from './Card'
import { DegenByGen } from './charts/DegenByGen'
import { DegradationVsStress, type TierCurve } from './charts/DegradationVsStress'
import { EpochsStrip, type EpochBlock } from './charts/EpochsStrip'
import { FalseClaimRate, type ClaimRow } from './charts/FalseClaimRate'
import { GossipGraph, type GossipEdgeView, type GossipNode } from './charts/GossipGraph'
import { PopulationTimeline, type PopPoint } from './charts/PopulationTimeline'
import { SpendGauges } from './charts/SpendGauges'
import { TeffHistogram } from './charts/TeffHistogram'
import { WealthChart, type WealthRow } from './charts/WealthChart'
import { Chronicle } from './Chronicle'
import { ControlForms } from './ControlForms'
import { EventFeed } from './EventFeed'
import { Lineage } from './Lineage'
import { TaskBoard } from './TaskBoard'

const SpendCard = memo(function SpendCard() {
  const { spendToday, spendTotal, spawnPool } = useStore(useShallow((s) => ({ spendToday: s.world.spendToday, spendTotal: s.world.spendTotal, spawnPool: s.world.spawnPool })))
  const { dailyCap, totalCap } = useStore(useShallow((s) => ({ dailyCap: s.config?.daily_cap_usd ?? 0, totalCap: s.config?.total_cap_usd ?? 0 })))
  const poolMax = useMemo(() => Math.max(spawnPool, 5), [spawnPool])
  return (
    <Card id="spend" title="Spend" meta={<span className="muted small tnum">caps {dailyCap.toFixed(2)} / {totalCap.toFixed(2)}</span>}>
      <SpendGauges spendToday={spendToday} spendTotal={spendTotal} spawnPool={spawnPool} dailyCap={dailyCap} totalCap={totalCap} spawnPoolMax={poolMax} />
    </Card>
  )
})

const WealthCard = memo(function WealthCard() {
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

const TeffCard = memo(function TeffCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const config = useStore((s) => s.config)
  const mode = config?.degeneration_mode
  const label = mode === 'observe' ? 'T_eff (prompt-visible; observed)' : 'T_eff (prompt-visible; induced)'
  const bins = useMemo(() => {
    void rev
    return Int32Array.from(series.teffBins)
  }, [rev, series])
  const collapse = useMemo(() => {
    void rev
    const out: Array<{ tier: string; tC: number; color: string }> = []
    const seen = new Set<string>()
    for (const [tier, tc] of Object.entries(config?.tiers ?? {})) {
      if (typeof tc.collapse_temperature === 'number') {
        out.push({ tier, tC: tc.collapse_temperature, color: selectTierColor(config, tier) })
        seen.add(tier)
      }
    }
    for (const [tier, tC] of series.tCByTier) if (!seen.has(tier)) out.push({ tier, tC, color: selectTierColor(config, tier) })
    return out
  }, [rev, series, config])
  return (
    <Card id="teff" title={label} meta={<span className="muted small">last 240 ticks</span>}>
      <TeffHistogram bins={bins} samples={series.teffSamples} collapse={collapse} label={label} />
    </Card>
  )
})

const DegradationCard = memo(function DegradationCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const config = useStore((s) => s.config)
  const curves = useMemo(() => {
    void rev
    const out: TierCurve[] = []
    for (const [tier, acc] of series.coherenceByTierByStressBin) {
      const coherence: number[] = []
      const invalid: number[] = []
      const n: number[] = []
      for (let b = 0; b < STRESS_BINS; b++) {
        coherence.push(acc.cohN[b]! > 0 ? acc.cohSum[b]! / acc.cohN[b]! : NaN)
        invalid.push(acc.invN[b]! > 0 ? acc.invSum[b]! / acc.invN[b]! : NaN)
        n.push(Math.max(acc.cohN[b]!, acc.invN[b]!))
      }
      out.push({ tier, color: selectTierColor(config, tier), coherence, invalid, n })
    }
    out.sort((a, b) => a.tier.localeCompare(b.tier))
    return out
  }, [rev, series, config])
  return (
    <Card id="degradation" title="Observed degradation vs stress" meta={<span className="muted small">{series.metricsRows} metric rows</span>}>
      <DegradationVsStress curves={curves} />
    </Card>
  )
})

const DegenCard = memo(function DegenCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const counts = useMemo(() => {
    void rev
    const n = Math.min(MAX_GENERATIONS, Math.max(1, series.maxGeneration + 1))
    return Array.from(series.degenByGen.subarray(0, n))
  }, [rev, series])
  return (
    <Card id="degen" title="Degeneration events per generation">
      <DegenByGen counts={counts} total={series.degenTotal} />
    </Card>
  )
})

const PopulationCard = memo(function PopulationCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const { ticksPerDay, cap } = useStore(useShallow((s) => ({ ticksPerDay: s.config?.ticks_per_day ?? 24, cap: s.config?.population_cap ?? 12 })))
  const renderTick = useStore((s) => s.clock.tick)
  const gens = Math.min(MAX_GENERATIONS, Math.max(1, series.maxGeneration + 1))
  const points = useMemo(() => {
    void rev
    const n = series.popByGenCount
    const step = Math.max(1, Math.ceil(n / 240))
    const out: PopPoint[] = []
    for (let i = 0; i < n; i += step) {
      const { tick, byGen } = series.popByGenAt(i)
      out.push({ tick, byGen: Array.from(byGen.subarray(0, gens)) })
    }
    if (n > 0 && (n - 1) % step !== 0) {
      const { tick, byGen } = series.popByGenAt(n - 1)
      out.push({ tick, byGen: Array.from(byGen.subarray(0, gens)) })
    }
    return out
  }, [rev, series, gens])
  return (
    <Card id="population" title="Population & generations">
      <PopulationTimeline points={points} generations={gens} ticksPerDay={ticksPerDay} renderTick={renderTick} populationCap={cap} />
    </Card>
  )
})

const ClaimsCard = memo(function ClaimsCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const config = useStore((s) => s.config)
  const rows = useMemo(() => {
    void rev
    const out: ClaimRow[] = []
    for (const [tier, c] of series.falseClaimByTier) out.push({ tier, color: selectTierColor(config, tier), made: c.made, falsy: c.falsy })
    out.sort((a, b) => a.tier.localeCompare(b.tier))
    return out
  }, [rev, series, config])
  return (
    <Card id="claims" title="False-claim rate by tier">
      <FalseClaimRate rows={rows} />
    </Card>
  )
})

const GossipCard = memo(function GossipCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const rosterById = useStore((s) => s.rosterById)
  const config = useStore((s) => s.config)
  const { selectedId, select } = useStore(useShallow((s) => ({ selectedId: s.selectedAgentId, select: s.select })))
  const { nodes, edges } = useMemo(() => {
    void rev
    const ids = new Set<string>()
    const edges: GossipEdgeView[] = []
    for (const e of series.gossipEdges.values()) {
      ids.add(e.from)
      ids.add(e.to)
      edges.push({ from: e.from, to: e.to, count: e.count })
    }
    const nodes: GossipNode[] = Array.from(ids)
      .map((id) => {
        const a = rosterById.get(id)
        return { id, name: a?.name ?? id, color: selectTierColor(config, a?.tier ?? ''), alive: a?.status === 'alive' }
      })
      .sort((a, b) => a.name.localeCompare(b.name))
    return { nodes, edges }
  }, [rev, series, rosterById, config])
  return (
    <Card id="gossip" title="Gossip graph">
      <GossipGraph nodes={nodes} edges={edges} total={series.gossipTotal} selectedId={selectedId} onSelect={select} />
    </Card>
  )
})

const EpochsCard = memo(function EpochsCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const epochs = useStore((s) => s.world.epochs)
  const day = useStore((s) => s.clock.day)
  const blocks = useMemo(() => {
    void rev
    const out: EpochBlock[] = []
    const applied = new Map<string, EpochBlock>()
    for (const e of series.epochs) {
      const key = `${e.kind}@${e.day}`
      if (e.phase === 'ended' || e.phase === 'expire') {
        const b = applied.get(key) ?? [...applied.values()].reverse().find((x) => x.kind === e.kind && x.state === 'active')
        if (b) {
          b.endDay = e.day
          b.state = 'past'
        }
        continue
      }
      const b: EpochBlock = {
        key: 'ev' + e.seq,
        kind: e.kind,
        startDay: e.day,
        endDay: e.day + (e.duration_days ?? 1),
        state: 'active',
        detail: [e.scarcity != null ? `scarcity ${e.scarcity}` : '', e.weather_baseline != null ? `weather ${e.weather_baseline}` : ''].filter(Boolean).join(' · ') || e.kind,
      }
      applied.set(key, b)
      out.push(b)
    }
    for (const b of out) if (b.state === 'active' && b.endDay <= day) b.state = 'past'
    for (const s of epochs.scheduled) {
      out.push({ key: `sched-${s.kind}-${s.day}`, kind: s.kind, startDay: s.day, endDay: s.day + (s.duration_days ?? 1), state: 'scheduled', detail: s.scarcity != null ? `scarcity ${s.scarcity}` : s.weather_baseline != null ? `weather ${s.weather_baseline}` : s.kind })
    }
    return out
  }, [rev, series, epochs, day])
  const maxDay = Math.max(day + 2, ...blocks.map((b) => b.endDay))
  return (
    <Card id="epochs" title="Epochs" meta={epochs.active ? <span className="pill warn">{epochs.active.kind} active</span> : undefined}>
      <EpochsStrip blocks={blocks} currentDay={day} maxDay={maxDay} />
    </Card>
  )
})

const AgentCard = memo(function AgentCard() {
  const id = useStore((s) => s.selectedAgentId)
  const name = useStore((s) => (s.selectedAgentId ? (s.rosterById.get(s.selectedAgentId)?.name ?? s.selectedAgentId) : null))
  return (
    <Card id="agent" title={name ? `Agent · ${name}` : 'Selected agent'} meta={id ? <span className="muted small">{id}</span> : undefined}>
      <AgentPanel />
    </Card>
  )
})

const FeedCard = memo(function FeedCard() {
  const total = useStore((s) => s.events.size)
  const rev = useStore((s) => s.eventsRev)
  void rev
  return (
    <Card id="feed" title="Event feed" meta={<span className="muted small tnum">{total} buffered</span>}>
      <EventFeed />
    </Card>
  )
})

export const ControlRoom = memo(function ControlRoom() {
  return (
    <div className="control-room-inner">
      <SpendCard />
      <AgentCard />
      <FeedCard />
      <WealthCard />
      <TeffCard />
      <DegradationCard />
      <DegenCard />
      <PopulationCard />
      <ClaimsCard />
      <GossipCard />
      <EpochsCard />
      <Card id="chronicle" title="Chronicle">
        <Chronicle />
      </Card>
      <Card id="tasks" title="Task board">
        <TaskBoard />
      </Card>
      <Card id="controls" title="Controls" defaultOpen={false}>
        <ControlForms />
      </Card>
      <Card id="lineage" title="Lineage">
        <Lineage />
      </Card>
    </div>
  )
})
