import { memo } from 'react'
import { Card } from '../Card'
import { EmptyState } from './EmptyState'

/** Memory-layer view; registered only on runs with the memory layer enabled. Placeholder for now. */
export const MemoryCard = memo(function MemoryCard() {
  return (
    <Card id="memory" title="Memory">
      <EmptyState />
    </Card>
  )
})
