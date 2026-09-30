/**
 * Splits agent-authored text into plain text runs and [[wikilinks]]. The
 * output is data for a React renderer that emits text nodes and <span>s only:
 * no HTML is ever parsed, and links are never hrefs.
 */
export interface TextSegment {
  kind: 'text'
  text: string
}
export interface LinkSegment {
  kind: 'link'
  /** Link target as written, trimmed; `[[Title|alias]]` keeps the title as target. */
  target: string
  /** Display text (alias when present). */
  label: string
}
export type NoteSegment = TextSegment | LinkSegment

const WIKILINK = /\[\[([^\[\]\n]{1,120})\]\]/g

export function parseNoteText(input: string): NoteSegment[] {
  const text = typeof input === 'string' ? input : ''
  const out: NoteSegment[] = []
  const pushText = (t: string) => {
    if (!t) return
    const prev = out[out.length - 1]
    if (prev && prev.kind === 'text') prev.text += t
    else out.push({ kind: 'text', text: t })
  }
  let last = 0
  WIKILINK.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = WIKILINK.exec(text)) !== null) {
    if (m.index > last) pushText(text.slice(last, m.index))
    const inner = m[1]!
    const bar = inner.indexOf('|')
    const target = (bar >= 0 ? inner.slice(0, bar) : inner).trim()
    const label = (bar >= 0 ? inner.slice(bar + 1) : inner).trim()
    if (target.length === 0) pushText(m[0])
    else out.push({ kind: 'link', target, label: label || target })
    last = m.index + m[0].length
  }
  if (last < text.length) pushText(text.slice(last))
  return out
}
