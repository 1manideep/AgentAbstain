import { describe, expect, it } from 'vitest'
import { bubbleCharBudget, clampBubbleText } from './bubbleText'

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
