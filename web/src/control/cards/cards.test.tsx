import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { HelloConfig } from '../../protocol'
import { CARDS, ControlRoom, visibleCards } from '../ControlRoom'
import { AdoptionCard } from './AdoptionCard'
import { DailyCard } from './DailyCard'
import { EMPTY_TEXT } from './EmptyState'
import { GadgetsCard } from './GadgetsCard'
import { MemoryCard } from './MemoryCard'
import { VaultCard } from './VaultCard'

/** The control room's cards before the split, in display order; placeholders follow. */
const LEGACY_IDS = ['spend', 'agent', 'feed', 'wealth', 'teff', 'degradation', 'degen', 'population', 'claims', 'gossip', 'epochs', 'chronicle', 'tasks', 'controls', 'lineage']
const PLACEHOLDER_IDS = ['daily', 'memory', 'vault', 'gadgets', 'adoption']
const MEMORY_ONLY = new Set(['memory', 'vault', 'adoption'])

function cfg(memory_layer?: HelloConfig['memory_layer']): HelloConfig {
  const base = {
    name: 't',
    seed: 1,
    days: 1,
    ticks_per_day: 24,
    tick_seconds: 0,
    render_delay_ticks: 0,
    world_size: 32,
    population_cap: 12,
    daily_cap_usd: 1,
    total_cap_usd: 1,
    tiers: {},
  }
  return (memory_layer === undefined ? base : { ...base, memory_layer }) as HelloConfig
}

const layer = (enabled: boolean, policy: boolean) => ({ enabled, policy, maintenance: false, exam: false })
const shown = (c: HelloConfig | null) => visibleCards(c).map((e) => e.id)

/** Card reads `void.card.<id>` from localStorage to restore its open state; recording those reads tells which card
 *  ids a render produced, in order. */
let reads: string[] = []
beforeEach(() => {
  reads = []
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k: string) => {
        reads.push(k)
        return null
      },
      setItem: () => {},
    },
  })
})
afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage
})
const cardIds = () => reads.filter((k) => k.startsWith('void.card.')).map((k) => k.slice('void.card.'.length))

describe('card registry', () => {
  it('keeps the existing cards in their order, with the placeholders last', () => {
    expect(CARDS.map((c) => c.id)).toEqual([...LEGACY_IDS, ...PLACEHOLDER_IDS])
    expect(new Set(CARDS.map((c) => c.id)).size).toBe(CARDS.length)
  })

  it('gates only the memory cards', () => {
    for (const c of CARDS) expect(c.when !== undefined).toBe(MEMORY_ONLY.has(c.id))
  })

  it('hides memory and vault unless memory_layer.enabled, and adoption unless policy is on', () => {
    const always = [...LEGACY_IDS, 'daily', 'gadgets']
    expect(shown(null)).toEqual(always)
    expect(shown(cfg())).toEqual(always)
    expect(shown(cfg(layer(false, false)))).toEqual(always)

    const enabled = shown(cfg(layer(true, false)))
    expect(enabled).toContain('memory')
    expect(enabled).toContain('vault')
    expect(enabled).not.toContain('adoption')

    const both = shown(cfg(layer(true, true)))
    expect(both).toEqual([...LEGACY_IDS, ...PLACEHOLDER_IDS])

    const policyOnly = shown(cfg(layer(false, true)))
    expect(policyOnly).toContain('adoption')
    expect(policyOnly).not.toContain('memory')
    expect(policyOnly).not.toContain('vault')
  })

  it('keeps registry order when filtering', () => {
    const order = CARDS.map((c) => c.id)
    for (const c of [null, cfg(layer(true, false)), cfg(layer(false, true)), cfg(layer(true, true))]) {
      const ids = shown(c)
      const idx = ids.map((id) => order.indexOf(id))
      expect(idx).toEqual([...idx].sort((a, b) => a - b))
    }
  })

  it('each entry renders the card its id names', () => {
    for (const { id, Component } of CARDS) {
      reads = []
      const html = renderToStaticMarkup(<Component />)
      expect(html).toContain('class="card')
      expect(cardIds()[0]).toBe(id)
    }
  })

  it('the control room renders the ungated cards in registry order before hello', () => {
    const html = renderToStaticMarkup(<ControlRoom />)
    expect(html.startsWith('<div class="control-room-inner">')).toBe(true)
    expect(cardIds()).toEqual(shown(null))
  })
})

describe('placeholder cards', () => {
  it('render an empty state under their title', () => {
    const cases: Array<[string, () => string]> = [
      ['Daily', () => renderToStaticMarkup(<DailyCard />)],
      ['Memory', () => renderToStaticMarkup(<MemoryCard />)],
      ['Vault', () => renderToStaticMarkup(<VaultCard />)],
      ['Gadgets', () => renderToStaticMarkup(<GadgetsCard />)],
      ['Adoption', () => renderToStaticMarkup(<AdoptionCard />)],
    ]
    for (const [title, render] of cases) {
      const html = render()
      expect(html).toContain(`<h2 class="card-title">${title}</h2>`)
      expect(html).toContain(EMPTY_TEXT)
    }
    expect(EMPTY_TEXT).toBe('no data yet')
  })
})
