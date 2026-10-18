/** Speech-bubble text budget: compact bubbles wrap to at most BUBBLE_LINES lines, cut at a word with an ellipsis. */
export const BUBBLE_FONT = 0.27
export const BUBBLE_WIDTH = 1.7
export const BUBBLE_LINES = 3
export const BUBBLE_LINE_HEIGHT = 1.2

/** The ellipsis the clamp helpers append: three ASCII dots, never the single ellipsis character. */
export const ELLIPSIS = '...'

/** Zero-width characters: dropped outright (U+FEFF counts as whitespace to a regex, so it is listed here first). */
const ZERO_WIDTH = new Set([0x200b, 0x200c, 0x200d, 0x2060, 0xfeff])
const WHITESPACE = /\s/

function foldChar(ch: string): string {
  const code = ch.codePointAt(0) ?? 0
  if (code >= 0x20 && code <= 0x7e) return ch
  // full-width ASCII (the kernel rewrites < and > to these)
  if (code >= 0xff01 && code <= 0xff5e) return String.fromCharCode(code - 0xfee0)
  // curly single quotes, then curly double quotes (including the low and reversed forms)
  if (code >= 0x2018 && code <= 0x201b) return "'"
  if (code >= 0x201c && code <= 0x201f) return '"'
  // hyphens, en and em dashes, the horizontal bar and the minus sign
  if ((code >= 0x2010 && code <= 0x2015) || code === 0x2212) return '-'
  if (code === 0x2026) return ELLIPSIS
  if (code === 0xb7) return '-'
  if (ZERO_WIDTH.has(code)) return ''
  // NBSP and every other space, tab or line break: one plain space (the clamps collapse runs)
  if (WHITESPACE.test(ch)) return ' '
  return ''
}

/**
 * Text for a troika <Text>: printable ASCII only (0x20-0x7E). A glyph the bundled font lacks (an emoji, CJK, a
 * pencil, a ballot X) makes the text renderer fetch fallback fonts from a CDN, which the page's content security
 * policy blocks. NFKD-normalises and strips combining marks (accented Latin keeps its base letter), maps curly
 * quotes, dashes, the ellipsis, full-width ASCII, NBSP and the middle dot to ASCII, drops zero-width characters,
 * and drops everything else.
 */
export function toTroikaText(s: string): string {
  const decomposed = String(s ?? '')
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
  let out = ''
  for (const ch of decomposed) out += foldChar(ch)
  return out
}

/** Folded, whitespace-collapsed, trimmed and capped at `max` characters. */
function foldAndCollapse(raw: unknown, max: number): string {
  return toTroikaText(String(raw ?? ''))
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, max)
}

/** At most budget + 1 characters: cut at a word (or hard-cut a giant word), no dangling punctuation, then '...'. */
function cutAtWord(text: string, budget: number): string {
  if (text.length <= budget) return text
  const room = budget + 1 - ELLIPSIS.length
  const cut = text.lastIndexOf(' ', room)
  const head = text.slice(0, cut >= room * 0.6 ? cut : room).replace(/[\s,;:.]+$/, '')
  return head + ELLIPSIS
}

export function bubbleCharBudget(): number {
  const perLine = Math.floor(BUBBLE_WIDTH / (BUBBLE_FONT * 0.5))
  return perLine * BUBBLE_LINES - 1
}

/** A speech bubble's text: folded to printable ASCII, whitespace-collapsed and cut at a word with '...'. */
export function clampBubbleText(raw: unknown): string {
  return cutAtWord(foldAndCollapse(raw, 280), bubbleCharBudget())
}

/** Note cards (remembering, revising the self, nightly maintenance): smaller, narrower, at most NOTE_LINES lines. */
export const NOTE_FONT = BUBBLE_FONT * 0.8
export const NOTE_WIDTH = 1.4
export const NOTE_LINES = 2

export function noteCharBudget(): number {
  const perLine = Math.floor(NOTE_WIDTH / (NOTE_FONT * 0.5))
  return perLine * NOTE_LINES - 1
}

/**
 * A note card's text: "noted: " plus the title, folded to printable ASCII (see toTroikaText), whitespace-collapsed
 * and cut at a word with '...'. A title that folds to nothing reads "a thought".
 */
export function clampNoteText(raw: unknown): string {
  const title = foldAndCollapse(raw, 200)
  return cutAtWord(`noted: ${title || 'a thought'}`, noteCharBudget())
}
