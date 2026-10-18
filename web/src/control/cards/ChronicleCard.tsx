import { memo } from 'react'
import { Card } from '../Card'
import { Chronicle } from '../Chronicle'

export const ChronicleCard = memo(function ChronicleCard() {
  return (
    <Card id="chronicle" title="Chronicle">
      <Chronicle />
    </Card>
  )
})
