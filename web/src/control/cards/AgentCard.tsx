import { memo } from 'react'
import { useStore } from '../../state/store'
import { AgentPanel } from '../AgentPanel'
import { Card } from '../Card'

export const AgentCard = memo(function AgentCard() {
  const id = useStore((s) => s.selectedAgentId)
  const name = useStore((s) => (s.selectedAgentId ? (s.rosterById.get(s.selectedAgentId)?.name ?? s.selectedAgentId) : null))
  return (
    <Card id="agent" title={name ? `Agent · ${name}` : 'Selected agent'} meta={id ? <span className="muted small">{id}</span> : undefined}>
      <AgentPanel />
    </Card>
  )
})
