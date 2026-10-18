import { memo } from 'react'
import { Card } from '../Card'
import { Lineage } from '../Lineage'

export const LineageCard = memo(function LineageCard() {
  return (
    <Card id="lineage" title="Lineage">
      <Lineage />
    </Card>
  )
})
