import { isValidElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { NoteText, renderNoteText } from './NoteText'
import { parseNoteText } from './noteText'
import { wordDiff } from './wordDiff'

const HOSTILE = [
  'Look: <img src=x onerror="alert(1)"> then [[x]] and more',
  '<script>alert("pwn")</script> [[Node n3|the rich node]]',
  'click [javascript:alert(1)](javascript:alert(1)) or <a href="javascript:alert(1)">here</a>',
  '[[ ]] empty and [[unterminated',
  'nested [[a [[b]] c]]',
  '<b>bold</b> &amp; entities &lt;script&gt;',
]

describe('note renderer', () => {
  it('produces only text nodes and wikilink spans', () => {
    for (const src of HOSTILE) {
      const nodes = renderNoteText(src, () => {})
      for (const n of nodes) {
        if (typeof n === 'string') continue
        expect(isValidElement(n)).toBe(true)
        const el = n as ReactElement<{ className?: string; children?: unknown; href?: unknown; dangerouslySetInnerHTML?: unknown }>
        expect(el.type).toBe('span')
        expect(String(el.props.className)).toContain('wikilink')
        expect(typeof el.props.children).toBe('string')
        expect(el.props.href).toBeUndefined()
        expect(el.props.dangerouslySetInnerHTML).toBeUndefined()
      }
    }
  })

  it('escapes markup so no tag from the source survives into HTML', () => {
    for (const src of HOSTILE) {
      const html = renderToStaticMarkup(<NoteText text={src} onLink={() => {}} />)
      expect(html).not.toMatch(/<img/i)
      expect(html).not.toMatch(/<script/i)
      expect(html).not.toMatch(/<a\b/i)
      expect(html).not.toMatch(/<[a-z]+[^>]*\shref=/i)
      if (src.includes('javascript:')) expect(html).toContain('&lt;a href=&quot;javascript:')
      expect(html).not.toMatch(/<b>/i)
      // hostile markup is escaped into inert text
      if (src.includes('<img')) expect(html).toContain('&lt;img')
      if (src.includes('<script')) expect(html).toContain('&lt;script')
      // the only elements are the wrapper and wikilink spans
      const tags = html.match(/<([a-z]+)/gi) ?? []
      for (const t of tags) expect(t.toLowerCase()).toBe('<span')
    }
  })

  it('parses wikilinks with aliases and leaves broken ones as text', () => {
    expect(parseNoteText('a [[x]] b')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'link', target: 'x', label: 'x' },
      { kind: 'text', text: ' b' },
    ])
    expect(parseNoteText('[[Node n3|the rich node]]')).toEqual([{ kind: 'link', target: 'Node n3', label: 'the rich node' }])
    expect(parseNoteText('[[ ]] and [[open')).toEqual([{ kind: 'text', text: '[[ ]] and [[open' }])
    expect(parseNoteText('')).toEqual([])
  })

  it('renders wikilink text as the label, with the target as data', () => {
    const html = renderToStaticMarkup(<NoteText text="see [[Node n3|the rich node]]" onLink={() => {}} />)
    expect(html).toContain('the rich node')
    expect(html).toContain('data-target="Node n3"')
    expect(html).toContain('role="button"')
  })
})

describe('word diff', () => {
  it('marks added and removed words', () => {
    const d = wordDiff('I am careful and quiet', 'I am restless and loud today')
    const kinds = d.map((t) => t.kind + ':' + t.text)
    expect(kinds).toEqual(['same:I am', 'del:careful', 'add:restless', 'same:and', 'del:quiet', 'add:loud today'])
  })
  it('handles empty sides', () => {
    expect(wordDiff('', 'a b')).toEqual([{ kind: 'add', text: 'a b' }])
    expect(wordDiff('a b', '')).toEqual([{ kind: 'del', text: 'a b' }])
  })
})
