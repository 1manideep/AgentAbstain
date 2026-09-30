import { memo, useCallback, useState, type ReactNode } from 'react'

interface CardProps {
  id: string
  title: string
  meta?: ReactNode
  defaultOpen?: boolean
  children: ReactNode
}

function readOpen(id: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(`void.card.${id}`)
    return v === null ? fallback : v === '1'
  } catch {
    return fallback
  }
}

/** Collapsible card; open state persists per card in localStorage. */
export const Card = memo(function Card({ id, title, meta, defaultOpen = true, children }: CardProps) {
  const [open, setOpen] = useState(() => readOpen(id, defaultOpen))
  const toggle = useCallback(() => {
    setOpen((o) => {
      try {
        localStorage.setItem(`void.card.${id}`, o ? '0' : '1')
      } catch {
        /* ignore */
      }
      return !o
    })
  }, [id])
  return (
    <section className={'card' + (open ? ' open' : '')}>
      <header className="card-head" onClick={toggle}>
        <button type="button" className="card-toggle" aria-expanded={open} aria-label={open ? 'Collapse' : 'Expand'}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        <h2 className="card-title">{title}</h2>
        {meta !== undefined ? <div className="card-meta">{meta}</div> : null}
      </header>
      {open ? <div className="card-body">{children}</div> : null}
    </section>
  )
})
