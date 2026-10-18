import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DegenerationMode, Epoch, EventMsg, MetricsGroup, MetricsRow, RosterAgent, SnapshotMsg, StressBin } from '../../protocol'
import { DEGRADATION_BINS, Series } from '../../state/series'
import { CalibrationNote, DegradationVsStress } from './DegradationVsStress'
import { CALIBRATION_LABEL, calibrationLabel, dayStep, degradationView, epochBlocks, MAX_EPOCH_LANES, pointLabel } from './degradationEpochsModel'
import { EpochsStrip } from './EpochsStrip'

const roster = new Map<string, RosterAgent>()
const color = (tier: string) => (tier === 'genius' ? '#3987e5' : '#d95926')

let seq = 0
function epochEv(day: number, payload: Record<string, unknown>): EventMsg {
  seq++
  return { type: 'event', seq, tick: day * 24 + 1, day, kind: 'epoch', agent_id: null, visibility: 'public', payload } as EventMsg
}

function snap(tick: number, day: number, active: Epoch | null, scheduled: Epoch[] = []): SnapshotMsg {
  return {
    type: 'snapshot',
    tick,
    day,
    tick_of_day: tick % 24,
    ts_ms: tick * 500,
    paused: false,
    weather: 0,
    scarcity: 1,
    spend_today_usd: 0,
    spend_total_usd: 0,
    spawn_pool_usd: 0,
    population: 0,
    gadgets_rev: 0,
    tasks_rev: 0,
    chronicle_rev: 0,
    epochs: { active, scheduled },
    agents: [],
    nodes: [],
  }
}

function row(tick: number, byTier: Record<string, Partial<MetricsGroup>>): MetricsRow {
  const by_tier: Record<string, MetricsGroup> = {}
  for (const [t, g] of Object.entries(byTier)) by_tier[t] = { population: 2, ...g }
  return { run_id: 'r', seed: 1, config_hash: 'h', experiment: null, arm: null, tick, day: 0, population: 2, by_tier, by_generation: {} }
}

function bins(over: Record<number, Partial<StressBin>>): StressBin[] {
  return Array.from({ length: DEGRADATION_BINS }, (_, k) => ({ lo: k / 10, calls: 0, coherence_sum: 0, coherence_n: 0, invalid_sum: 0, ...over[k] }))
}

describe('calibration label', () => {
  it('labels induce and both as a calibration check and leaves observe unlabelled', () => {
    const modes: Array<[DegenerationMode, string | null]> = [
      ['induce', CALIBRATION_LABEL],
      ['both', CALIBRATION_LABEL],
      ['observe', null],
    ]
    for (const [degeneration_mode, want] of modes) expect(calibrationLabel({ degeneration_mode })).toBe(want)
    expect(CALIBRATION_LABEL).toBe('induced: calibration check, not a finding')
  })

  it('treats a config without the field as the server default (both) and says nothing before hello', () => {
    expect(calibrationLabel({})).toBe(CALIBRATION_LABEL)
    expect(calibrationLabel(null)).toBeNull()
  })

  it('renders the note only when induced', () => {
    expect(renderToStaticMarkup(<CalibrationNote config={{ degeneration_mode: 'induce' }} />)).toContain(CALIBRATION_LABEL)
    expect(renderToStaticMarkup(<CalibrationNote config={{ degeneration_mode: 'both' }} />)).toContain(CALIBRATION_LABEL)
    expect(renderToStaticMarkup(<CalibrationNote config={{ degeneration_mode: 'observe' }} />)).toBe('')
  })
})

describe('degradation vs per-call stress', () => {
  it('keeps calls at stress 0.1 and 0.9 in separate bins even when the tier mean stress is 0.5', () => {
    const s = new Series()
    // one agent calls at stress 0.1 and coheres, another at 0.9 and does not; the tier mean stress is 0.5
    s.onMetrics(
      row(1, {
        genius: {
          mean_stress: 0.5,
          text_coherence_mean: 0.55,
          invalid_action_rate: 0.5,
          stress_bins: bins({ 1: { calls: 1, coherence_sum: 0.9, coherence_n: 1, invalid_sum: 0 }, 9: { calls: 1, coherence_sum: 0.2, coherence_n: 1, invalid_sum: 1 } }),
        },
      }),
    )
    const v = degradationView(s, color)
    expect(v.source).toBe('calls')
    expect(v.bins).toBe(DEGRADATION_BINS)
    expect(v.total).toBe(2)
    expect(v.curves).toHaveLength(1)
    const g = v.curves[0]!
    expect(g.tier).toBe('genius')
    expect(g.color).toBe('#3987e5')
    expect(g.coherence[1]).toBeCloseTo(0.9, 10)
    expect(g.coherence[9]).toBeCloseTo(0.2, 10)
    expect(g.invalid[1]).toBe(0)
    expect(g.invalid[9]).toBe(1)
    expect(g.n.filter((n) => n > 0)).toEqual([1, 1])
    for (const b of [0, 2, 3, 4, 5, 6, 7, 8]) {
      expect(g.coherence[b]).toBeNaN()
      expect(g.invalid[b]).toBeNaN()
    }

    // the old structure would have merged both calls into the bin of the mean stress
    const old = degradationView({ degradationBins: new Map(), coherenceByTierByStressBin: s.coherenceByTierByStressBin }, color)
    expect(old.source).toBe('rows')
    expect(old.curves[0]!.n.filter((n) => n > 0)).toEqual([1])
    expect(old.curves[0]!.coherence[2]).toBeCloseTo(0.55, 10)
  })

  it('weights each bin by calls across rows, not by rows', () => {
    const s = new Series()
    s.onMetrics(row(1, { genius: { stress_bins: bins({ 3: { calls: 3, coherence_sum: 2.4, coherence_n: 3, invalid_sum: 0 } }) } }))
    s.onMetrics(row(2, { genius: { stress_bins: bins({ 3: { calls: 1, coherence_sum: 0.2, coherence_n: 1, invalid_sum: 1 } }) }, dim: { stress_bins: bins({ 3: { calls: 2, coherence_sum: 0.4, coherence_n: 1, invalid_sum: 2 } }) } }))
    const v = degradationView(s, color)
    expect(v.curves.map((c) => c.tier)).toEqual(['dim', 'genius'])
    const [dim, genius] = v.curves
    // (2.4 + 0.2) / 4 calls, not the mean of the two rows' means (0.8 + 0.2) / 2
    expect(genius!.coherence[3]).toBeCloseTo(0.65, 10)
    expect(genius!.invalid[3]).toBeCloseTo(0.25, 10)
    // coherence counts scored calls only; the invalid rate counts every call
    expect(dim!.coherence[3]).toBeCloseTo(0.4, 10)
    expect(dim!.invalid[3]).toBe(1)
    expect(v.total).toBe(6)
  })

  it('shows the call counts in the tooltip label', () => {
    const s = new Series()
    s.onMetrics(row(1, { dim: { stress_bins: bins({ 6: { calls: 5, coherence_sum: 1.5, coherence_n: 3, invalid_sum: 1 }, 7: { calls: 1, coherence_sum: 0, coherence_n: 0, invalid_sum: 0 } }) } }))
    const v = degradationView(s, color)
    const dim = v.curves[0]!
    expect(pointLabel(dim, 6, 'coherence', v.source)).toBe('dim · 3 scored of 5 calls')
    expect(pointLabel(dim, 6, 'invalid', v.source)).toBe('dim · 5 calls')
    expect(pointLabel(dim, 7, 'invalid', v.source)).toBe('dim · 1 call')
    // a bin with calls but no scored call has an invalid rate and no coherence
    expect(dim.coherence[7]).toBeNaN()
    expect(dim.invalid[7]).toBe(0)
  })

  it('falls back to the mean-stress structure when no row carried per-call bins', () => {
    const s = new Series()
    s.onMetrics(row(1, { genius: { mean_stress: 0.7, text_coherence_mean: 0.6, invalid_action_rate: 0.1 } }))
    s.onMetrics(row(2, { genius: { mean_stress: 0.75, text_coherence_mean: 0.4, invalid_action_rate: 0.3 } }))
    const v = degradationView(s, color)
    expect(v.source).toBe('rows')
    expect(v.bins).toBe(5)
    expect(v.total).toBe(2)
    const g = v.curves[0]!
    expect(g.coherence[3]).toBeCloseTo(0.5, 10)
    expect(g.invalid[3]).toBeCloseTo(0.2, 10)
    expect(pointLabel(g, 3, 'coherence', v.source)).toBe('genius · 2 rows')
    const html = renderToStaticMarkup(<DegradationVsStress view={v} />)
    expect(html).toContain('tier mean stress per tick')
  })

  it('leaves out tiers without data and waits when there is nothing to plot', () => {
    const s = new Series()
    // a tier with population but no calls yet
    s.onMetrics(row(1, { genius: { stress_bins: bins({}) } }))
    const v = degradationView(s, color)
    expect(v.curves).toEqual([])
    expect(renderToStaticMarkup(<DegradationVsStress view={v} />)).toContain('Waiting for metrics rows.')
  })

  it('draws one line per run of filled bins, never across an empty bin', () => {
    const s = new Series()
    s.onMetrics(
      row(1, {
        genius: {
          stress_bins: bins({
            1: { calls: 1, coherence_sum: 0.9, coherence_n: 1 },
            2: { calls: 1, coherence_sum: 0.8, coherence_n: 1 },
            9: { calls: 1, coherence_sum: 0.2, coherence_n: 1 },
          }),
        },
      }),
    )
    const html = renderToStaticMarkup(<DegradationVsStress view={degradationView(s, color)} />)
    expect(html).toContain('x: stress of each decide call, 10 bins')
    // two panels: each draws one path for bins 1-2; bin 9 stands alone as a point
    const paths = html.match(/<path d="M[^"]+" fill="none" stroke="#3987e5"/g) ?? []
    expect(paths).toHaveLength(2)
    expect((html.match(/r="4" fill="#3987e5"/g) ?? []).length).toBe(6)
  })
})

describe('epochs strip model', () => {
  it('draws started(day 2, ends_day 5) as an active block over days 2-5 on day 3, past after ended', () => {
    const s = new Series()
    s.onEvent(epochEv(2, { kind: 'drought', id: 'cfg0', phase: 'started', ends_day: 5, scarcity: 0.4, weather_baseline: null, arrival_agent_id: null }), roster)
    s.onSnapshot(snap(3 * 24, 3, { id: 'cfg0', kind: 'drought', day: 2, ends_day: 5, duration_days: 3, scarcity: 0.4 }), roster)
    const open = epochBlocks(s.epochSpans(), { active: { id: 'cfg0', kind: 'drought', day: 2, ends_day: 5, duration_days: 3 }, scheduled: [] })
    expect(open).toHaveLength(1)
    expect(open[0]).toMatchObject({ key: 'cfg0', id: 'cfg0', kind: 'drought', startDay: 2, endDay: 5, state: 'active', lane: 0, detail: 'scarcity 0.4' })

    s.onEvent(epochEv(5, { kind: 'drought', id: 'cfg0', phase: 'ended' }), roster)
    const done = epochBlocks(s.epochSpans(), { active: null, scheduled: [] })
    expect(done).toHaveLength(1)
    expect(done[0]).toMatchObject({ id: 'cfg0', startDay: 2, endDay: 5, state: 'past' })
  })

  it('stays active until its own ended event, not the end of another epoch of the same kind', () => {
    const s = new Series()
    s.onEvent(epochEv(1, { kind: 'storm', id: 'cfg0', phase: 'started', ends_day: 3 }), roster)
    s.onEvent(epochEv(2, { kind: 'storm', id: 'op1', phase: 'started', ends_day: 8 }), roster)
    s.onEvent(epochEv(3, { kind: 'storm', id: 'cfg0', phase: 'ended' }), roster)
    const blocks = epochBlocks(s.epochSpans(), { active: null, scheduled: [] })
    expect(blocks.map((b) => [b.id, b.startDay, b.endDay, b.state])).toEqual([
      ['cfg0', 1, 3, 'past'],
      ['op1', 2, 8, 'active'],
    ])
    // they overlap on day 2, so they take separate rows
    expect(blocks.map((b) => b.lane)).toEqual([0, 1])
  })

  it('draws an epoch already active at page load', () => {
    const s = new Series()
    const active: Epoch = { id: 'op1', kind: 'storm', day: 3, duration_days: 1, ends_day: 7, weather_baseline: -0.5, scarcity: null }
    s.onSnapshot(snap(4 * 24, 4, active), roster)
    const blocks = epochBlocks(s.epochSpans(), { active, scheduled: [] })
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toMatchObject({ id: 'op1', kind: 'storm', startDay: 3, endDay: 7, state: 'active', detail: 'weather -0.5' })
  })

  it('draws the snapshot active epoch even when the series has no span for it', () => {
    const active: Epoch = { id: 'op4', kind: 'boom', day: 6, duration_days: 1, ends_day: 9 }
    expect(epochBlocks([], { active, scheduled: [] })).toMatchObject([{ id: 'op4', startDay: 6, endDay: 9, state: 'active' }])
    // without an id it is matched by kind and first day
    const s = new Series()
    s.onEvent(epochEv(6, { kind: 'boom', id: 'op4', phase: 'started', ends_day: 9 }), roster)
    expect(epochBlocks(s.epochSpans(), { active: { kind: 'boom', day: 6, duration_days: 3, ends_day: 9 }, scheduled: [] })).toHaveLength(1)
  })

  it('draws scheduled epochs over their duration and drops one that has already started', () => {
    const s = new Series()
    s.onEvent(epochEv(4, { kind: 'drought', id: 'op1', phase: 'started', ends_day: 6 }), roster)
    const scheduled: Epoch[] = [
      { id: 'op1', kind: 'drought', day: 4, duration_days: 2 },
      { id: 'op2', kind: 'arrival', day: 9, duration_days: 3 },
    ]
    const blocks = epochBlocks(s.epochSpans(), { active: null, scheduled })
    expect(blocks.map((b) => [b.key, b.startDay, b.endDay, b.state])).toEqual([
      ['op1', 4, 6, 'active'],
      ['sched-op2', 9, 12, 'scheduled'],
    ])
  })

  it('keeps an ended event whose start was never seen, at least one lane, and at most MAX_EPOCH_LANES lanes', () => {
    const s = new Series()
    s.onEvent(epochEv(9, { kind: 'drought', id: 'cfg3', phase: 'ended' }), roster)
    expect(epochBlocks(s.epochSpans(), { active: null, scheduled: [] })).toMatchObject([{ id: 'cfg3', startDay: 9, endDay: 9, state: 'past', detail: 'start not seen' }])

    const many: Epoch[] = Array.from({ length: MAX_EPOCH_LANES + 2 }, (_, i) => ({ id: `op${i}`, kind: 'storm', day: 1, duration_days: 5 }))
    const lanes = epochBlocks([], { active: null, scheduled: many }).map((b) => b.lane)
    expect(Math.max(...lanes)).toBe(MAX_EPOCH_LANES - 1)
    expect(epochBlocks([], { active: null, scheduled: many }, 0).every((b) => b.lane === 0)).toBe(true)
  })

  it('renders one rect per block and thins the day labels on long runs', () => {
    const blocks = epochBlocks([], { active: { id: 'cfg0', kind: 'drought', day: 2, ends_day: 5, duration_days: 3 }, scheduled: [{ id: 'op1', kind: 'storm', day: 40, duration_days: 5 }] })
    const html = renderToStaticMarkup(<EpochsStrip blocks={blocks} currentDay={3.5} />)
    expect((html.match(/<rect /g) ?? []).length).toBe(2)
    expect(html).toContain('drought')
    expect(html).toContain('storm')
    expect(dayStep(5)).toBe(1)
    expect(dayStep(45)).toBe(5)
    expect(dayStep(400)).toBe(50)
    expect(renderToStaticMarkup(<EpochsStrip blocks={[]} currentDay={0} />)).toContain('No epochs applied or scheduled.')
  })
})
