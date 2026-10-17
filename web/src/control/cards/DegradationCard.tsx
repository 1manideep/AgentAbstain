import { memo, useMemo } from 'react'
import { STRESS_BINS } from '../../state/series'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { DegradationVsStress, type TierCurve } from '../charts/DegradationVsStress'

export const DegradationCard = memo(function DegradationCard() {
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
