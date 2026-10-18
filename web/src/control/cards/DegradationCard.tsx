import { memo, useMemo } from 'react'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { CalibrationNote, DegradationVsStress } from '../charts/DegradationVsStress'
import { degradationView } from '../charts/degradationEpochsModel'

export const DegradationCard = memo(function DegradationCard() {
  const rev = useStore((s) => s.seriesRev)
  const series = useStore((s) => s.series)
  const config = useStore((s) => s.config)
  const view = useMemo(() => {
    void rev
    return degradationView(series, (tier) => selectTierColor(config, tier))
  }, [rev, series, config])
  const meta = view.source === 'calls' ? `${view.total} calls · ${series.metricsRows} rows` : `${series.metricsRows} metric rows`
  return (
    <Card id="degradation" title="Observed degradation vs stress" meta={<span className="muted small">{meta}</span>}>
      <CalibrationNote config={config} />
      <DegradationVsStress view={view} />
    </Card>
  )
})
