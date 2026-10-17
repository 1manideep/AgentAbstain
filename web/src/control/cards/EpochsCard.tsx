import { memo, useMemo } from 'react'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { EpochsStrip, type EpochBlock } from '../charts/EpochsStrip'

export const EpochsCard = memo(function EpochsCard() {
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
