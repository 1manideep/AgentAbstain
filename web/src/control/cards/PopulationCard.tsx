import { memo, useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { MAX_GENERATIONS } from '../../state/series'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { PopulationTimeline, type PopPoint } from '../charts/PopulationTimeline'

export const PopulationCard = memo(function PopulationCard() {
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
