import { describe, expect, it } from 'vitest'
import { bubbleCharBudget, clampBubbleText, clampNoteText, noteCharBudget } from './bubbleText'

describe('speech bubble text', () => {
  it('keeps short lines, collapses whitespace and never exceeds the three-line budget', () => {
    const budget = bubbleCharBudget()
    expect(budget).toBeGreaterThan(20)
    expect(clampBubbleText('  hello   there\n friend ')).toBe('hello there friend')
    const long = 'The node n6 is rich. Weather is mild. My purse is comfortable. I will sleep early and see tomorrow.'
    const out = clampBubbleText(long)
    expect(out.length).toBeLessThanOrEqual(budget + 1)
    expect(out.endsWith('…')).toBe(true)
    // cut on a word boundary, no dangling punctuation before the ellipsis
    expect(out).not.toMatch(/[ ,.;:]…$/)
    expect(long.startsWith(out.slice(0, -1))).toBe(true)
  })
  it('hard-cuts a single giant word and tolerates non-strings', () => {
    const out = clampBubbleText('x'.repeat(400))
    expect(out.length).toBe(bubbleCharBudget() + 1)
    expect(clampBubbleText(null)).toBe('')
    expect(clampBubbleText(42)).toBe('42')
  })
})

describe('clampNoteText', () => {
  it('prefixes a pencil, collapses whitespace and keeps short titles whole', () => {
    expect(clampNoteText('  Node n3   is rich ')).toBe('✎ Node n3 is rich')
    expect(clampNoteText('')).toBe('✎ a note')
    expect(clampNoteText(undefined)).toBe('✎ a note')
  })

  it('cuts long titles at a word with an ellipsis within the two-line budget', () => {
    const long = 'Foraging at node n3 on day 4 after the storm when Bao said the north node was empty again'
    const out = clampNoteText(long)
    expect(out.length).toBeLessThanOrEqual(noteCharBudget() + 1)
    expect(out.endsWith('…')).toBe(true)
    expect(out.startsWith('✎ Foraging')).toBe(true)
    expect(out.slice(0, -1).endsWith(' ')).toBe(false)
    expect(noteCharBudget()).toBeLessThan(bubbleCharBudget())
  })
})
