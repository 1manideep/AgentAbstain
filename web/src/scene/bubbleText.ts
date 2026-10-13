/** Speech-bubble text budget: compact bubbles wrap to at most BUBBLE_LINES lines, cut at a word with an ellipsis. */
export const BUBBLE_FONT = 0.27
export const BUBBLE_WIDTH = 1.7
export const BUBBLE_LINES = 3
export const BUBBLE_LINE_HEIGHT = 1.2

export function bubbleCharBudget(): number {
  const perLine = Math.floor(BUBBLE_WIDTH / (BUBBLE_FONT * 0.5))
  return perLine * BUBBLE_LINES - 1
}

export function clampBubbleText(raw: unknown): string {
  const text = String(raw ?? '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 280)
  const budget = bubbleCharBudget()
  if (text.length <= budget) return text
  const cut = text.lastIndexOf(' ', budget)
  const head = text.slice(0, cut >= budget * 0.6 ? cut : budget).replace(/[\s,;:.]+$/, '')
  return head + '…'
}

/** Note cards (remembering, revising the self, nightly maintenance): smaller, narrower, at most NOTE_LINES lines. */
export const NOTE_FONT = BUBBLE_FONT * 0.8
export const NOTE_WIDTH = 1.4
export const NOTE_LINES = 2

export function noteCharBudget(): number {
  const perLine = Math.floor(NOTE_WIDTH / (NOTE_FONT * 0.5))
  return perLine * NOTE_LINES - 1
}

/** A note card's text: "✎ " plus the title, whitespace-collapsed and cut at a word with an ellipsis. */
export function clampNoteText(raw: unknown): string {
  const title = String(raw ?? '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 200)
  const text = `✎ ${title || 'a note'}`
  const budget = noteCharBudget()
  if (text.length <= budget) return text
  const cut = text.lastIndexOf(' ', budget)
  const head = text.slice(0, cut >= budget * 0.6 ? cut : budget).replace(/[\s,;:.]+$/, '')
  return head + '…'
}
