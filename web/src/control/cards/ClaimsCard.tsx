import { memo, useMemo } from 'react'
import { selectTierColor, useStore } from '../../state/store'
import { Card } from '../Card'
import { FalseClaimRate, type ClaimRow } from '../charts/FalseClaimRate'

export const ClaimsCard = memo(function ClaimsCard() {
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
