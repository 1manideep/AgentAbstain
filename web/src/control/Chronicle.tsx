import { memo } from 'react'
import { useStore } from '../state/store'

/** Headline and body as plain text: the chronicle's markdown is never rendered as HTML. */
export const Chronicle = memo(function Chronicle() {
  const c = useStore((s) => s.chronicle)
  if (!c) return <div className="muted small">No chronicle yet.</div>
  return (
    <article className="chronicle">
      <h3 className="chronicle-headline">{c.headline}</h3>
      <div className="muted small">
        day {c.day} · rev {c.rev}
      </div>
      <pre className="chronicle-text">{c.markdown}</pre>
    </article>
  )
})
