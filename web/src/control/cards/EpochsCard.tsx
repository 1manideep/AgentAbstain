import { memo, useMemo } from 'react'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { epochBlocks } from '../charts/degradationEpochsModel'
import { EpochsStrip } from '../charts/EpochsStrip'

export const EpochsCard = memo(function EpochsCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const epochs = useStore((s) => s.world.epochs)
  const day = useStore((s) => s.clock.day)
  const tickOfDay = useStore((s) => s.clock.tickOfDay)
  const ticksPerDay = useStore((s) => s.config?.ticks_per_day ?? 0)
  const blocks = useMemo(() => {
    void rev
    return epochBlocks(series.epochSpans(), epochs)
  }, [rev, series, epochs])
  const active = Array.from(new Set(blocks.filter((b) => b.state === 'active').map((b) => b.kind)))
  const dayPos = ticksPerDay > 0 ? day + Math.min(1, Math.max(0, tickOfDay / ticksPerDay)) : day
  return (
    <Card id="epochs" title="Epochs" meta={active.length ? <span className="pill warn">{active.join(' + ')} active</span> : undefined}>
      <EpochsStrip blocks={blocks} currentDay={dayPos} />
    </Card>
  )
})
