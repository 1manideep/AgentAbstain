import { memo, type ReactNode } from 'react'
import { parseNoteText } from './noteText'

export interface NoteTextProps {
  text: string
  /** Called with the link target; the panel decides whether a matching note exists. */
  onLink?: (target: string) => void
  /** Targets that resolve to a note; others render as inert spans. */
  resolves?: (target: string) => boolean
}

/**
 * Renders agent text as React text children plus clickable <span>s for
 * [[wikilinks]]. Never HTML: `<img onerror>` or `<script>` in the source
 * become literal characters on screen.
 */
export function renderNoteText(text: string, onLink?: (t: string) => void, resolves?: (t: string) => boolean): ReactNode[] {
  return parseNoteText(text).map((seg, i) => {
    if (seg.kind === 'text') return seg.text
    const ok = resolves ? resolves(seg.target) : true
    return (
      <span
        key={i}
        className={'wikilink' + (ok ? '' : ' unresolved')}
        role={ok ? 'button' : undefined}
        tabIndex={ok ? 0 : undefined}
        data-target={seg.target}
        onClick={ok && onLink ? () => onLink(seg.target) : undefined}
        onKeyDown={
          ok && onLink
            ? (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onLink(seg.target)
                }
              }
            : undefined
        }
      >
        {seg.label}
      </span>
    )
  })
}

export const NoteText = memo(function NoteText({ text, onLink, resolves }: NoteTextProps) {
  return <span className="note-text">{renderNoteText(text, onLink, resolves)}</span>
})
