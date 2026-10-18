import { describe, expect, it } from 'vitest'
import { bubbleCharBudget, clampBubbleText, clampNoteText, ELLIPSIS, noteCharBudget, toTroikaText } from './bubbleText'

/** Every character is printable ASCII (0x20-0x7E): the only glyphs troika may draw without fetching fonts. */
function expectPrintableAscii(s: string): void {
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    expect(c >= 0x20 && c <= 0x7e, `U+${c.toString(16).toUpperCase().padStart(4, '0')} in ${JSON.stringify(s)}`).toBe(true)
  }
}

const HOSTILE = [
  '“Curly” ‘quotes’ „here‟',
  'an em dash — and an en dash – here',
  'wait… then …',
  '＜script＞ alert(1) ＜/script＞',
  'node n3 is rich \u{1F389}\u{1F680} \u{1F44D}\u{1F3FD} go',
  '節点は豊かです node 한국어',
  'Café crème brûlée Ångström naïve señor',
  'a\u{A0}b\u{A0}c\u{202F}d',
  'zero\u{200B}width\u{200C}join\u{200D}here\u{2060}x\u{FEFF}y',
  'Bao · elder · g3',
  'tab\there\nnew line\r\nend',
]

describe('toTroikaText', () => {
  it('maps the listed characters to their ASCII forms', () => {
    expect(toTroikaText('“Curly” ‘quotes’')).toBe('"Curly" \'quotes\'')
    expect(toTroikaText('a — b – c')).toBe('a - b - c')
    expect(toTroikaText('wait…')).toBe('wait...')
    expect(toTroikaText('＜b＞ ！～Ａｚ０')).toBe('<b> !~Az0')
    expect(toTroikaText('a\u{A0}b')).toBe('a b')
    expect(toTroikaText('zero\u{200B}width\u{200C}\u{200D}\u{2060}\u{FEFF}!')).toBe('zerowidth!')
    expect(toTroikaText('Bao · elder')).toBe('Bao - elder')
  })

  it('keeps the base letter of accented Latin and drops emoji and CJK', () => {
    expect(toTroikaText('Café crème brûlée Ångström naïve señor')).toBe('Cafe creme brulee Angstrom naive senor')
    expect(toTroikaText('go \u{1F680}\u{1F44D}\u{1F3FD}!')).toBe('go !')
    expect(toTroikaText('節点は豊かです')).toBe('')
    expect(toTroikaText('한국어')).toBe('')
  })

  it('leaves printable ASCII untouched and is idempotent', () => {
    let ascii = ''
    for (let c = 0x20; c <= 0x7e; c++) ascii += String.fromCharCode(c)
    expect(toTroikaText(ascii)).toBe(ascii)
    for (const s of HOSTILE) {
      const once = toTroikaText(s)
      expectPrintableAscii(once)
      expect(toTroikaText(once)).toBe(once)
    }
  })

  it('turns other whitespace into spaces and drops control characters', () => {
    expect(toTroikaText('tab\there\nline')).toBe('tab here line')
    expect(toTroikaText('bell\u0007del\u007f')).toBe('belldel')
  })
})

describe('speech bubble text', () => {
  it('keeps short lines, collapses whitespace and never exceeds the three-line budget', () => {
    const budget = bubbleCharBudget()
    expect(budget).toBeGreaterThan(20)
    expect(clampBubbleText('  hello   there\n friend ')).toBe('hello there friend')
    const long = 'The node n6 is rich. Weather is mild. My purse is comfortable. I will sleep early and see tomorrow.'
    const out = clampBubbleText(long)
    expect(out.length).toBeLessThanOrEqual(budget + 1)
    expect(out.endsWith(ELLIPSIS)).toBe(true)
    expect(ELLIPSIS).toBe('...')
    const head = out.slice(0, -ELLIPSIS.length)
    // cut on a word boundary, no dangling punctuation before the ellipsis
    expect(head).not.toMatch(/[ ,.;:]$/)
    expect(long.startsWith(head)).toBe(true)
    expect(long.charAt(head.length)).toMatch(/[ .,;:]/)
  })

  it('hard-cuts a single giant word and tolerates non-strings', () => {
    const out = clampBubbleText('x'.repeat(400))
    expect(out.length).toBe(bubbleCharBudget() + 1)
    expect(out.endsWith('x...')).toBe(true)
    expect(clampBubbleText(null)).toBe('')
    expect(clampBubbleText(42)).toBe('42')
  })

  it('folds hostile text to printable ASCII within the budget', () => {
    const budget = bubbleCharBudget()
    const inputs = [...HOSTILE, HOSTILE.join(' '), '…'.repeat(200), '＜'.repeat(300), '\u{1F600}'.repeat(300), 'e\u{301}'.repeat(300)]
    for (const raw of inputs) {
      const out = toTroikaText(clampBubbleText(raw))
      expectPrintableAscii(out)
      expect(out.length).toBeLessThanOrEqual(budget + 1)
      // the clamp already folds, so the display-time fold changes nothing
      expect(out).toBe(clampBubbleText(raw))
      expect(out).toBe(out.trim())
      expect(out).not.toMatch(/ {2}/)
    }
    expect(clampBubbleText('＜b＞ “hi” — ok…')).toBe('<b> "hi" - ok...')
    expect(clampBubbleText('\u{1F600}\u{1F600}')).toBe('')
  })
})

describe('clampNoteText', () => {
  it('prefixes "noted:", collapses whitespace and keeps short titles whole', () => {
    expect(clampNoteText('  Node n3   is rich ')).toBe('noted: Node n3 is rich')
    expect(clampNoteText('')).toBe('noted: a thought')
    expect(clampNoteText(undefined)).toBe('noted: a thought')
  })

  it('uses only characters the bundled font covers (no CDN fallback fonts)', () => {
    for (const t of [clampNoteText('Foraging at node n3 on day 4 after the storm when Bao said the north node was empty again'), clampNoteText('x')]) {
      expectPrintableAscii(t)
    }
  })

  it('cuts long titles at a word with an ellipsis within the two-line budget', () => {
    const long = 'Foraging at node n3 on day 4 after the storm when Bao said the north node was empty again'
    const out = clampNoteText(long)
    expect(out.length).toBeLessThanOrEqual(noteCharBudget() + 1)
    expect(out.endsWith(ELLIPSIS)).toBe(true)
    expect(out.startsWith('noted: Foraging')).toBe(true)
    expect(out.slice(0, -ELLIPSIS.length).endsWith(' ')).toBe(false)
    expect(noteCharBudget()).toBeLessThan(bubbleCharBudget())
  })

  it('folds hostile titles to printable ASCII within the budget', () => {
    const budget = noteCharBudget()
    for (const raw of [...HOSTILE, HOSTILE.join(' '), '…'.repeat(100)]) {
      const out = toTroikaText(clampNoteText(raw))
      expectPrintableAscii(out)
      expect(out.length).toBeLessThanOrEqual(budget + 1)
      // (a title of only dots hard-cuts to 'noted' plus the ellipsis: trailing punctuation is never kept)
      expect(out.startsWith('noted')).toBe(true)
    }
    expect(clampNoteText('‘N3’ is rich')).toBe("noted: 'N3' is rich")
    // a title that folds to nothing still reads as a note
    expect(clampNoteText('節点 \u{1F389}')).toBe('noted: a thought')
  })
})
