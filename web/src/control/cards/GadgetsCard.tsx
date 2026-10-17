import { memo } from 'react'
import { Card } from '../Card'
import { EmptyState } from './EmptyState'

/** Gadgets agents have written and used. Placeholder for now. */
export const GadgetsCard = memo(function GadgetsCard() {
  return (
    <Card id="gadgets" title="Gadgets">
      <EmptyState />
    </Card>
  )
})
