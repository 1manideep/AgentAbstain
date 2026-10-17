import { memo } from 'react'
import { useStore } from '../../state/store'
import { Card } from '../Card'
import { EventFeed } from '../EventFeed'

export const FeedCard = memo(function FeedCard() {
  const total = useStore((s) => s.events.size)
  const rev = useStore((s) => s.eventsRev)
  void rev
  return (
    <Card id="feed" title="Event feed" meta={<span className="muted small tnum">{total} buffered</span>}>
      <EventFeed />
    </Card>
  )
})
