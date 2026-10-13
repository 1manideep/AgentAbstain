import { describe, expect, it } from 'vitest'
import type { EventMsg, RosterAgent } from '../protocol'
import { FEED_KINDS, summarize } from './EventFeed'

const roster = new Map<string, RosterAgent>([
  ['a', { id: 'a', name: 'Ada', tier: 'genius', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' }],
  ['b', { id: 'b', name: 'Bao', tier: 'dim', generation: 0, parent_id: null, born_tick: 0, died_tick: null, status: 'alive' }],
])
let seq = 0
const ev = (kind: string, payload: Record<string, unknown>, agent_id: string | null = 'a'): EventMsg =>
  ({ type: 'event', seq: ++seq, tick: 3, day: 1, kind, agent_id, visibility: 'public', payload }) as EventMsg

describe('memory-layer event summaries', () => {
  it('reads like sentences, with names instead of ids', () => {
    expect(summarize(ev('remember', { agent_id: 'a', note_id: 'n1', title: 'Node n3 is rich', channel: 'observed' }), roster)).toBe('Ada noted “Node n3 is rich”')
    expect(summarize(ev('remember', { agent_id: 'a', title: 'x', channel: 'chronicle' }), roster)).toBe('Ada noted “x” (chronicle)')
    expect(summarize(ev('maintenance', { agent_id: 'a', commands: 3, ok: 2, refused: 1, strategy: 'index_builder', world_cost: 1500 }), roster))
      .toMatch(/^Ada tidied memory: 2 of 3 changes, 1 refused · index_builder \(/)
    expect(summarize(ev('maintenance_skipped', { agent_id: 'b', reason: 'insufficient' }, 'b'), roster)).toBe('Bao could not afford to tidy memory (insufficient)')
    expect(summarize(ev('claim_feedback', { speaker_id: 'b', listener_id: 'a', claim: { subject: 'node', id: 'n3', attr: 'stock_bucket', value: 'rich' } }), roster))
      .toBe('Ada found out Bao lied: “node n3 stock bucket = rich”')
    expect(summarize(ev('policy_changed', { agent_id: 'a', version: 2, source: 'self_edit', similarity_to_parent: 0.25, chars: 400 }), roster))
      .toBe('Ada memory policy v2 (self_edit, 25% like the last, 400 chars)')
    expect(summarize(ev('memory_command', { agent_id: 'a', command: 'create', path: '/memories/index/Node-n3-index.md', ok: true }), roster))
      .toBe('Ada create /memories/index/Node-n3-index.md ok')
    expect(summarize(ev('exam_answered', { agent_id: 'a', kind: 'node', correct: false, abstained: true }), roster)).toBe('Ada exam (node): abstained')
    expect(summarize(ev('share_note', { speaker_id: 'a', listener_id: 'b', note_title: 'Thin node' }), roster)).toBe('Ada → Bao shared “Thin node”')
  })

  it('offers the new kinds as filter chips', () => {
    for (const k of ['maintenance', 'claim_feedback', 'policy_changed', 'memory_command']) expect(FEED_KINDS).toContain(k)
  })
})
