import type { KeyboardEvent, MouseEvent, ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { CallRecord, NoteDetail, PolicyVersion } from '../protocol'
import { CallCostSplit, Persona } from './AgentPanel'
import { NoteProvenance, NoteProvenanceView } from './NoteProvenance'
import { AgentLink, PolicyHistory, PolicyHistoryList } from './PolicyHistory'

const NAMES: Record<string, string> = { a1: 'Cy', a7: 'Ada', a9: 'Bo' }
const nameOf = (id: string) => NAMES[id] ?? id
const noop = () => {}

const V1: PolicyVersion = {
  version: 1,
  tick: 0,
  source: 'seed',
  source_agent_id: null,
  parent_version: null,
  similarity_to_parent: null,
  text: '# Rules\n\n- keep notes short\n- link every note',
}
const V2: PolicyVersion = {
  version: 2,
  tick: 24,
  source: 'self_edit',
  source_agent_id: null,
  parent_version: 1,
  similarity_to_parent: 0.8,
  text: '# Rules\n\n- keep notes brief\n- link every note',
}
const V3: PolicyVersion = {
  version: 3,
  tick: 48,
  source: 'adopted',
  source_agent_id: 'a7',
  parent_version: 2,
  similarity_to_parent: 0.6667,
  text: '# Rules\n\n- keep notes brief\n- link every note\n- trust two sources',
}

/** The rendered list split into one chunk per <li>, in display order. */
function versionChunks(html: string): string[] {
  return html.split('<li').slice(1)
}

/** The diff spans of a chunk as "kind:text" (text still HTML-escaped). */
function diffSpans(chunk: string): string[] {
  return [...chunk.matchAll(/<span class="diff-(same|add|del)">([^<]*)<\/span>/g)].map((m) => `${m[1]}:${m[2]}`)
}

/** Every opening tag in the markup as [name, attribute names]; attribute values are skipped whole (quoted). */
function tags(html: string): Array<[string, string[]]> {
  return [...html.matchAll(/<([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>"]+(?:="[^"]*")?)*)\s*\/?>/g)].map((m) => [
    m[1]!.toLowerCase(),
    [...m[2]!.matchAll(/\s+([^\s=>"]+)(?:="[^"]*")?/g)].map((a) => a[1]!.toLowerCase()),
  ])
}

describe('policy history', () => {
  it('renders three versions newest first with their source tags, ticks and similarity', () => {
    // the endpoint answers newest first; an oldest-first list must come out the same way
    for (const versions of [
      [V3, V2, V1],
      [V1, V2, V3],
    ]) {
      const html = renderToStaticMarkup(<PolicyHistoryList versions={versions} nameOf={nameOf} onSelectAgent={noop} />)
      const chunks = versionChunks(html)
      expect(chunks).toHaveLength(3)
      expect(chunks.map((c) => /data-version="(\d+)"/.exec(c)?.[1])).toEqual(['3', '2', '1'])
      expect(chunks.map((c) => /data-source="([a-z_]+)"/.exec(c)?.[1])).toEqual(['adopted', 'self_edit', 'seed'])
      expect(chunks[0]).toContain('>adopted</span>')
      expect(chunks[1]).toContain('>self_edit</span>')
      expect(chunks[2]).toContain('>seed</span>')
      expect(chunks[0]).toContain('tick 48')
      expect(chunks[1]).toContain('tick 24')
      expect(chunks[2]).toContain('tick 0')
      expect(chunks[0]).toContain('sim 0.67')
      expect(chunks[1]).toContain('sim 0.80')
      expect(chunks[2]).not.toContain('sim ')
      // the newest version starts unfolded
      expect(chunks[0]).toContain('<details open=""')
      expect(chunks[1]).not.toContain('<details open')
    }
  })

  it('diffs each version against its parent version, keeping line breaks', () => {
    const chunks = versionChunks(renderToStaticMarkup(<PolicyHistoryList versions={[V3, V2, V1]} nameOf={nameOf} />))
    expect(diffSpans(chunks[0]!)).toEqual(['same:# Rules\n\n- keep notes brief\n- link every note', 'add:\n- trust two sources'])
    expect(chunks[0]).toContain('diff vs v2')
    expect(diffSpans(chunks[1]!)).toEqual(['same:# Rules\n\n- keep notes', 'del:short', 'add:brief', 'same:\n- link every note'])
    expect(chunks[1]).toContain('diff vs v1')
    // words are separated by a space, never across a line break
    expect(chunks[1]).toContain('keep notes</span> <span class="diff-del">short</span> <span class="diff-add">brief</span><span class="diff-same">\n- link')
    // the seed has no parent: full text, no diff spans
    expect(diffSpans(chunks[2]!)).toEqual([])
    expect(chunks[2]).toContain('first')
    expect(chunks[2]).toContain('# Rules\n\n- keep notes short\n- link every note')
  })

  it('links the source agent through the selection action', () => {
    const chunks = versionChunks(renderToStaticMarkup(<PolicyHistoryList versions={[V3, V2, V1]} nameOf={nameOf} onSelectAgent={noop} />))
    expect(chunks[0]).toMatch(/from <span class="wikilink" role="button" tabindex="0" data-agent-id="a7">Ada<\/span>/)
    expect(chunks[1]).not.toContain('data-agent-id')
    expect(chunks[2]).not.toContain('data-agent-id')

    // the link itself: click and Enter select the agent and keep the <summary> from folding
    const selected: string[] = []
    const inner = (AgentLink as unknown as { type: (p: { id: string; name: string; onSelect?: (id: string) => void }) => ReactElement }).type
    const el = inner({ id: 'a7', name: 'Ada', onSelect: (id) => selected.push(id) }) as ReactElement<{
      onClick: (e: MouseEvent) => void
      onKeyDown: (e: KeyboardEvent) => void
    }>
    let prevented = 0
    const ev = { preventDefault: () => prevented++, stopPropagation: noop }
    el.props.onClick(ev as unknown as MouseEvent)
    el.props.onKeyDown({ ...ev, key: 'Enter' } as unknown as KeyboardEvent)
    el.props.onKeyDown({ ...ev, key: 'x' } as unknown as KeyboardEvent)
    expect(selected).toEqual(['a7', 'a7'])
    expect(prevented).toBe(2)
    // without a selection action the name is plain text
    expect(renderToStaticMarkup(<AgentLink id="a7" name="Ada" />)).toBe('<span data-agent-id="a7">Ada</span>')
  })

  it('shows a copied first version and a missing parent as full text', () => {
    const inherited: PolicyVersion = { ...V1, version: 1, source: 'inherited', source_agent_id: 'a9', similarity_to_parent: 1 }
    const orphan: PolicyVersion = { ...V2, version: 5, parent_version: 4 }
    const chunks = versionChunks(renderToStaticMarkup(<PolicyHistoryList versions={[inherited, orphan]} nameOf={nameOf} onSelectAgent={noop} />))
    expect(chunks[0]).toContain('v4 not loaded')
    expect(diffSpans(chunks[0]!)).toEqual([])
    expect(chunks[1]).toContain('copied')
    expect(chunks[1]).toContain('data-agent-id="a9"')
    expect(chunks[1]).toContain('>Bo</span>')
    expect(chunks[1]).toContain('sim 1.00')
  })

  it('says so when there are no versions, and in fixture playback', () => {
    expect(renderToStaticMarkup(<PolicyHistoryList versions={[]} />)).toContain('No policy versions recorded.')
    expect(renderToStaticMarkup(<PolicyHistory agentId="a1" fixture />)).toContain('not recorded in fixture playback')
    // live: the fetch runs in an effect, so the first paint is the loading state
    expect(renderToStaticMarkup(<PolicyHistory agentId="a1" fixture={false} />)).toContain('loading')
  })
})

const NOTE: NoteDetail = {
  note_id: 'n-42',
  title: 'Node n3 is rich',
  body: 'Node n3 paid well. See [[Node n3]].',
  created_tick: 50,
  channel: 'gossip',
  hop: 2,
  importance: 0.5,
  archived: false,
  tags: ['node'],
  agent_id: 'a1',
  links: ['Node n3', 'Ada'],
  path: 'agents/a1/notes/node-n3-is-rich.md',
  source_agent_id: 'a7',
  origin_note_id: 'n-7',
  origin_generation: 1,
  transfer_tick: 48,
  path_agents: ['a9', 'a7'],
}

describe('note provenance', () => {
  it('renders a NoteDetail: source, origin note and generation, transfer tick, path and links', () => {
    const html = renderToStaticMarkup(
      <NoteProvenanceView detail={NOTE} nameOf={nameOf} onSelectAgent={noop} onLink={noop} resolves={(t) => t === 'Node n3'} />,
    )
    expect(html).toContain('data-note-id="n-42"')
    expect(html).toMatch(/<dt>source<\/dt><dd><span class="wikilink" role="button" tabindex="0" data-agent-id="a7">Ada<\/span><\/dd>/)
    expect(html).toContain('<dt>origin note</dt><dd class="tnum">n-7</dd>')
    expect(html).toContain('<dt>origin gen</dt><dd class="tnum">g1</dd>')
    expect(html).toContain('tick 48')
    expect(html).toContain('gossip · hop 2')
    // path: the givers in order, each clickable, then the holder
    const path = /<dt>path<\/dt><dd>(.*?)<\/dd>/.exec(html)?.[1] ?? ''
    expect([...path.matchAll(/data-agent-id="([^"]+)">([^<]+)</g)].map((m) => `${m[1]}=${m[2]}`)).toEqual(['a9=Bo', 'a7=Ada'])
    expect(path).toContain(' → ')
    expect(path).toContain('→ Cy</span>')
    // links: a resolvable one is a button, the other is inert
    expect(html).toContain('<span class="wikilink" role="button" tabindex="0" data-target="Node n3">Node n3</span>')
    expect(html).toContain('<span class="wikilink unresolved" data-target="Ada">Ada</span>')
    expect(html).toContain('agents/a1/notes/node-n3-is-rich.md')
  })

  it('shows dashes for a note without provenance', () => {
    const bare: NoteDetail = { ...NOTE, source_agent_id: null, origin_note_id: null, origin_generation: null, transfer_tick: null, path_agents: [], links: [] }
    const html = renderToStaticMarkup(<NoteProvenanceView detail={bare} />)
    expect(html).not.toContain('data-agent-id')
    expect(html.match(/<span class="muted">none<\/span>/g)?.length).toBe(6)
  })

  it('shows an empty state in fixture playback and loads otherwise', () => {
    expect(renderToStaticMarkup(<NoteProvenance agentId="a1" noteId="n-42" fixture />)).toContain('not recorded in fixture playback')
    expect(renderToStaticMarkup(<NoteProvenance agentId="a1" noteId="n-42" fixture={false} />)).toContain('loading provenance')
  })
})

const HOSTILE = '<script>alert("x")</script> <img src=x onerror="alert(1)"> <a href="javascript:alert(1)">go</a>'
const ALLOWED_TAGS = new Set(['ol', 'li', 'details', 'summary', 'span', 'p', 'dl', 'dt', 'dd', 'div', 'h3', 'table', 'thead', 'tbody', 'tr', 'th', 'td'])
const ALLOWED_ATTRS = new Set(['class', 'style', 'role', 'tabindex', 'title', 'open'])

function expectInert(html: string) {
  expect(html).not.toMatch(/<script/i)
  expect(html).not.toMatch(/<img/i)
  expect(html).not.toMatch(/<a\b/i)
  expect(html).toContain('&lt;script&gt;')
  expect(html).toContain('&lt;img')
  const found = tags(html)
  // every '<' in the markup opens one of the tags parsed here, so nothing escaped the text nodes
  expect(found.length).toBe(html.match(/<[a-zA-Z]/g)?.length ?? 0)
  for (const [name, attrs] of found) {
    expect(ALLOWED_TAGS.has(name)).toBe(true)
    // no href, src or on* handler: only presentation, role and data attributes
    for (const a of attrs) expect(ALLOWED_ATTRS.has(a) || a.startsWith('data-')).toBe(true)
  }
}

describe('hostile text renders as text', () => {
  it('in policy text, diffs, source tags and agent names', () => {
    const hostileName = () => HOSTILE
    const a: PolicyVersion = { ...V1, text: HOSTILE }
    const b: PolicyVersion = { ...V2, text: HOSTILE + ' and <b>more</b>' }
    const c = { ...V3, source: '"><img src=x onerror=alert(1)>', source_agent_id: '"><script>alert(2)</script>', text: '<script>x</script>' } as unknown as PolicyVersion
    const html = renderToStaticMarkup(<PolicyHistoryList versions={[a, b, c]} nameOf={hostileName} onSelectAgent={noop} />)
    expectInert(html)
    expect(html).not.toMatch(/<b>/i)
    expect(html).toContain('&lt;b&gt;more&lt;/b&gt;')
    // a hostile source lands in attributes escaped, never as markup
    expect(html).toContain('data-source="&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"')
  })

  it('in note provenance', () => {
    const evil: NoteDetail = {
      ...NOTE,
      channel: HOSTILE,
      origin_note_id: HOSTILE,
      links: [HOSTILE, '[[x]]'],
      path: HOSTILE,
      source_agent_id: HOSTILE,
      path_agents: [HOSTILE],
      agent_id: HOSTILE,
    }
    const html = renderToStaticMarkup(<NoteProvenanceView detail={evil} onSelectAgent={noop} onLink={noop} resolves={() => true} />)
    expectInert(html)
    expect(html).toContain('[[x]]')
  })

  it('in the motto, traits and call purposes', () => {
    const html = renderToStaticMarkup(
      <>
        <Persona motto={HOSTILE} traits={{ [HOSTILE]: 0.5 }} />
        <CallCostSplit calls={[call(HOSTILE, 0.01)]} />
      </>,
    )
    expectInert(html)
  })
})

function call(purpose: string | undefined, real: number, world?: number): CallRecord {
  const c: Partial<CallRecord> = { tick: 1, real_cost_usd: real, latency_ms: null, text_coherence: null, invalid_action: 0, degenerate_induced: 0 }
  if (purpose !== undefined) c.purpose = purpose
  if (world !== undefined) c.world_cost_usd = world
  return c as CallRecord
}

describe('call cost by purpose', () => {
  it('splits real and world cost into decide, maintain, exam and any other purpose present', () => {
    const calls = [call('decide', 0.01, 0.02), call('decide', 0.02, 0.03), call('maintain', 0.005, 0.01), call('gossip', 0.001, 0.002), call('chronicle', 0, 0.004)]
    const html = renderToStaticMarkup(<CallCostSplit calls={calls} />)
    const rows = [...html.matchAll(/<tr data-purpose="([^"]+)"[^>]*>(.*?)<\/tr>/g)].map((m) => [m[1], [...m[2]!.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((t) => t[1])])
    expect(rows).toEqual([
      ['decide', ['decide', '2', '$0.0300', '$0.0500']],
      ['maintain', ['maintain', '1', '$0.0050', '$0.0100']],
      ['exam', ['exam', '0', '$0.0000', '$0.0000']],
      ['chronicle', ['chronicle', '1', '$0.0000', '$0.0040']],
      ['gossip', ['gossip', '1', '$0.0010', '$0.0020']],
    ])
    expect(html).toContain('last 5 calls')
    // a purpose with no calls is muted
    expect(html).toContain('<tr data-purpose="exam" class="muted">')
  })

  it('drops the world column when calls carry no world cost', () => {
    const html = renderToStaticMarkup(<CallCostSplit calls={[call('decide', 0.01), call('exam', 0.002)]} />)
    expect(html).not.toContain('>world<')
    expect(html).toContain('<tr data-purpose="exam"><td')
  })

  it('labels the costs decide only when no call carries a purpose', () => {
    const html = renderToStaticMarkup(<CallCostSplit calls={[call(undefined, 0.01), call(undefined, 0.02)]} />)
    expect(html).toContain('data-purpose="decide-only"')
    expect(html).toContain('decide only · 2 calls · $0.0300 real')
    expect(html).not.toContain('<table')
    expect(renderToStaticMarkup(<CallCostSplit calls={[]} />)).toBe('')
  })
})

describe('persona', () => {
  it('shows the motto and the traits in server order', () => {
    const traits = { honesty: 0.9, greed: 0.25, curiosity: 0.5, caution: 1.4, industriousness: 0, sociability: 0.75, luck: 0.1 }
    const html = renderToStaticMarkup(<Persona motto="  Share what pays.  " traits={traits} />)
    expect(html).toContain('<p class="motto"')
    expect(html).toContain('“Share what pays.”')
    expect([...html.matchAll(/data-trait="([a-z]+)"/g)].map((m) => m[1])).toEqual([
      'greed',
      'sociability',
      'curiosity',
      'caution',
      'industriousness',
      'honesty',
      'luck',
    ])
    expect(html).toContain('data-trait="greed" style="display:block;height:100%;width:25%')
    // out-of-range values are clamped in the bar, shown as given
    expect(html).toContain('data-trait="caution" style="display:block;height:100%;width:100%')
    expect(html).toContain('1.40')
  })

  it('renders nothing without a motto or traits', () => {
    expect(renderToStaticMarkup(<Persona />)).toBe('')
    expect(renderToStaticMarkup(<Persona motto="  " traits={{}} />)).toBe('')
    expect(renderToStaticMarkup(<Persona motto="Keep moving" />)).not.toContain('traits')
  })
})
