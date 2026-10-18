import { memo } from 'react'

export const EMPTY_TEXT = 'no data yet'

/** The body a card shows before it has anything to plot. */
export const EmptyState = memo(function EmptyState({ text = EMPTY_TEXT }: { text?: string }) {
  return (
    <div className="muted small card-empty" style={{ padding: '12px 0' }}>
      {text}
    </div>
  )
})
