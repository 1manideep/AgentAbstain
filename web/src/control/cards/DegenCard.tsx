import { memo, useMemo } from 'react'
import { MAX_GENERATIONS } from '../../state/series'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { DegenByGen } from '../charts/DegenByGen'

export const DegenCard = memo(function DegenCard() {
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
