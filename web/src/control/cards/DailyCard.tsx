import { memo } from 'react'
import { Card } from '../Card'
import { EmptyState } from './EmptyState'

/** Per-day aggregates. Placeholder until the daily series is plotted here. */
export const DailyCard = memo(function DailyCard() {
  return (
    <Card id="daily" title="Daily">
      <EmptyState />
    </Card>
  )
})
