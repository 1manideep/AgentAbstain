import { memo } from 'react'
import { Card } from '../Card'
import { EmptyState } from './EmptyState'

/** Vault growth and structure; registered only on runs with the memory layer enabled. Placeholder for now. */
export const VaultCard = memo(function VaultCard() {
  return (
    <Card id="vault" title="Vault">
      <EmptyState />
    </Card>
  )
})
