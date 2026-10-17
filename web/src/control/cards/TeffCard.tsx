import { memo, useMemo } from 'react'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { TeffHistogram } from '../charts/TeffHistogram'

export const TeffCard = memo(function TeffCard() {
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
