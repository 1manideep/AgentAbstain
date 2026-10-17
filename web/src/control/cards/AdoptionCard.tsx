import { memo } from 'react'
import { Card } from '../Card'
import { EmptyState } from './EmptyState'

/** Practice adoption curves; registered only on runs with the policy file on. Placeholder for now. */
export const AdoptionCard = memo(function AdoptionCard() {
  return (
    <Card id="adoption" title="Adoption">
      <EmptyState />
    </Card>
  )
})
