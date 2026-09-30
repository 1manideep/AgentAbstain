import { describe, expect, it } from 'vitest'
import type { SnapshotAgent, SnapshotMsg } from '../protocol'
import {
  BIRTH_MS,
  CLAMP_FACTOR,
  History,
  SampleOut,
  STATUS_ALIVE,
  TOMBSTONE_MS,
} from './history'

function agent(id: string, x: number, y: number, extra: Partial<SnapshotAgent> = {}): SnapshotAgent {
  return {
    id,
    x,
    y,
    heading: 0,
    stress: 0.2,
    t_eff: 0.9,
    degenerate: false,
    asleep: false,
    status: 'alive',
    balance_usd: 1.5,
    anim: 'idle',
    last_action: null,
    effects: [],
    ...extra,
  }
}

function snap(tick: number, ts_ms: number, agents: SnapshotAgent[], stock = 20): SnapshotMsg {
  return {
    type: 'snapshot',
    tick,
    day: Math.floor(tick / 24),
    tick_of_day: tick % 24,
    ts_ms,
    paused: false,
    weather: 0,
    scarcity: 1,
    spend_today_usd: 0,
    spend_total_usd: 0,
    spawn_pool_usd: 5,
    population: agents.length,
    gadgets_rev: 0,
    tasks_rev: 0,
    chronicle_rev: 0,
    epochs: { active: null, scheduled: [] },
    agents,
    nodes: [{ id: 'n1', x: 10, y: 10, stock, capacity: 40, stock_delta: 0 }],
  }
}

function make(): { h: History; out: SampleOut } {
  const h = new History(64, 8, 4)
  const out = new SampleOut(8, 4)
  return { h, out }
}

describe('virtual timeline', () => {
  it('follows irregular intervals below the clamp and keeps positions linear', () => {
    const { h, out } = make()
    const deltas = [500, 1500, 300, 2500]
    let ts = 1_000_000
    h.push(snap(1, ts, [agent('a', 0, 0)]))
    let x = 0
    const expectVts = [0]
    for (let i = 0; i < deltas.length; i++) {
      ts += deltas[i]!
      x += 2
      h.push(snap(2 + i, ts, [agent('a', x, 0)]))
      expectVts.push(expectVts[i]! + deltas[i]!)
    }
    for (let i = 0; i < expectVts.length; i++) expect(h.frameAt(i).vts).toBeCloseTo(expectVts[i]!, 6)
    // Midpoint of the 1500 ms segment (frame 1 -> 2): x goes 2 -> 4.
    h.sample(500 + 750, out)
    const s = h.agentIndex.get('a')!
    expect(out.present[s]).toBe(1)
    expect(out.x[s]).toBeCloseTo(3, 5)
    expect(out.tick).toBe(2)
  })

  it('clamps a 20 s pause gap to 3·ema so interpolation never freezes', () => {
    const { h, out } = make()
    let ts = 0
    h.push(snap(1, ts, [agent('a', 0, 0)]))
    for (let i = 0; i < 6; i++) {
      ts += 1000
      h.push(snap(2 + i, ts, [agent('a', i + 1, 0)]))
    }
    const emaBefore = h.ema
    const before = h.newestVts
    ts += 20_000
    h.push(snap(8, ts, [agent('a', 10, 0)]))
    const step = h.newestVts - before
    expect(step).toBeCloseTo(CLAMP_FACTOR * emaBefore, 6)
    expect(step).toBeLessThan(20_000)
    // ema only grows by the clamped step.
    expect(h.ema).toBeLessThanOrEqual(emaBefore * (0.8 + 0.2 * CLAMP_FACTOR) + 1e-6)
    // Positions across the gap are continuous.
    const s = h.agentIndex.get('a')!
    let last = Number.NaN
    for (let v = before - 500; v <= h.newestVts; v += 25) {
      h.sample(v, out)
      if (!Number.isNaN(last)) expect(Math.abs(out.x[s]! - last)).toBeLessThan(0.2)
      last = out.x[s]!
    }
    expect(last).toBeCloseTo(10, 5)
  })

  it('tolerates missing ticks: samples across the gap and vtsOfTick reports unknown ticks', () => {
    const { h, out } = make()
    h.push(snap(1, 0, [agent('a', 0, 0)]))
    h.push(snap(2, 1000, [agent('a', 1, 0)]))
    h.push(snap(4, 2000, [agent('a', 3, 0)]))
    h.push(snap(5, 3000, [agent('a', 4, 0)]))
    expect(h.vtsOfTick(3)).toBeUndefined()
    expect(h.vtsOfTick(4)).toBe(2000)
    expect(h.indexAtTick(3)).toBe(2) // first frame with tick >= 3 is tick 4
    const s = h.agentIndex.get('a')!
    h.sample(1500, out)
    expect(out.x[s]).toBeCloseTo(2, 5)
    expect(out.tickA).toBe(2)
    expect(out.tickB).toBe(4)
  })
})

describe('births and deaths', () => {
  it('scales a birth in over 400 ms up to the first frame that holds it', () => {
    const { h, out } = make()
    h.push(snap(1, 0, [agent('a', 0, 0)]))
    h.push(snap(2, 1000, [agent('a', 1, 0)]))
    h.push(snap(3, 2000, [agent('a', 2, 0), agent('b', 5, 5)]))
    h.push(snap(4, 3000, [agent('a', 3, 0), agent('b', 6, 5)]))
    const sb = h.agentIndex.get('b')!
    // Before the scale-in window: present but scale 0, held at the birth position.
    h.sample(1200, out)
    expect(out.present[sb]).toBe(1)
    expect(out.scale[sb]).toBe(0)
    expect(out.x[sb]).toBeCloseTo(5, 5)
    // Halfway through the window.
    h.sample(2000 - BIRTH_MS / 2, out)
    expect(out.scale[sb]).toBeCloseTo(0.5, 5)
    expect(out.x[sb]).toBeCloseTo(5, 5)
    // At and after its first frame: full scale, interpolating normally.
    h.sample(2000, out)
    expect(out.scale[sb]).toBeCloseTo(1, 5)
    h.sample(2500, out)
    expect(out.scale[sb]).toBe(1)
    expect(out.x[sb]).toBeCloseTo(5.5, 5)
    // Before the birth segment entirely: absent.
    h.sample(500, out)
    expect(out.present[sb]).toBe(0)
  })

  it('holds a dead agent in place and fades it over 1 s, then removes it', () => {
    const { h, out } = make()
    h.push(snap(1, 0, [agent('a', 0, 0), agent('b', 5, 5)]))
    h.push(snap(2, 1000, [agent('a', 1, 0), agent('b', 6, 5)]))
    // Final frame as archived (§15 snapshot membership), then absent.
    h.push(snap(3, 2000, [agent('a', 2, 0), agent('b', 7, 5, { status: 'archived' })]))
    h.push(snap(4, 3000, [agent('a', 3, 0)]))
    h.push(snap(5, 4000, [agent('a', 4, 0)]))
    const sb = h.agentIndex.get('b')!
    // Walking to its final position before the death frame.
    h.sample(1500, out)
    expect(out.present[sb]).toBe(1)
    expect(out.dead[sb]).toBe(0)
    expect(out.x[sb]).toBeCloseTo(6.5, 5)
    expect(out.alpha[sb]).toBe(1)
    // Just after the death frame: dead, held at the final position, fading.
    h.sample(2250, out)
    expect(out.present[sb]).toBe(1)
    expect(out.dead[sb]).toBe(1)
    expect(out.x[sb]).toBeCloseTo(7, 5)
    expect(out.alpha[sb]).toBeCloseTo(0.75, 5)
    expect(out.ids[sb]).toBe('b')
    // Two segments later, still inside the tombstone window: last position is remembered.
    h.sample(2900, out)
    expect(out.present[sb]).toBe(1)
    expect(out.x[sb]).toBeCloseTo(7, 5)
    expect(out.alpha[sb]).toBeCloseTo(0.1, 5)
    // After the tombstone: gone.
    h.sample(2000 + TOMBSTONE_MS + 1, out)
    expect(out.present[sb]).toBe(0)
    // Its slot is reused only once no retained frame references it.
    h.push(snap(6, 5000, [agent('a', 5, 0), agent('c', 1, 1)]))
    expect(h.agentIndex.get('c')).not.toBe(sb)
  })

  it('treats a mid-segment death without a final frame as a tombstone at the newer frame', () => {
    const { h, out } = make()
    h.push(snap(1, 0, [agent('a', 0, 0), agent('b', 5, 5)]))
    h.push(snap(2, 1000, [agent('a', 1, 0)]))
    const sb = h.agentIndex.get('b')!
    h.sample(500, out)
    expect(out.present[sb]).toBe(1)
    expect(out.dead[sb]).toBe(0)
    expect(out.x[sb]).toBeCloseTo(5, 5)
    h.sample(1000, out)
    expect(out.dead[sb]).toBe(1)
    expect(out.alpha[sb]).toBeCloseTo(1, 5)
    expect(out.status[sb]).not.toBe(STATUS_ALIVE)
  })
})

describe('render clock', () => {
  function feed(h: History, n: number, dt = 1000): void {
    for (let i = 0; i < n; i++) h.push(snap(i + 1, i * dt, [agent('a', i, 0)]))
  }

  it('starts at the live target and stays a delay behind newest', () => {
    const { h } = make()
    feed(h, 30)
    h.snap()
    expect(h.playback.renderVts).toBeCloseTo(h.newestVts - 1.25 * h.ema, 6)
    for (let i = 0; i < 60; i++) h.advance(1 / 60)
    expect(h.playback.renderVts).toBeLessThanOrEqual(h.newestVts)
    expect(h.playback.renderVts).toBeGreaterThan(h.newestVts - 3 * h.ema)
  })

  it('snaps forward after a long gap in delivery', () => {
    const { h } = make()
    feed(h, 10)
    h.snap()
    const before = h.playback.renderVts
    // Nothing arrives for a while; the clock catches the newest frame and waits.
    for (let i = 0; i < 300; i++) h.advance(1 / 60)
    expect(h.playback.renderVts).toBeLessThanOrEqual(h.newestVts)
    expect(h.playback.renderVts).toBeGreaterThan(before)
    // A burst of frames arrives far ahead: one advance snaps to the new target.
    for (let i = 10; i < 40; i++) h.push(snap(i + 1, i * 1000, [agent('a', i, 0)]))
    const snapsBefore = h.playback.snaps
    h.advance(1 / 60)
    expect(h.playback.snaps).toBe(snapsBefore + 1)
    expect(h.playback.renderVts).toBeCloseTo(h.liveTarget(), 3)
  })

  it('keeps renderVts monotonic in live mode under speed changes and rate control', () => {
    const { h } = make()
    feed(h, 5)
    h.snap()
    let last = h.playback.renderVts
    let tick = 5
    let ts = 5000
    for (let i = 0; i < 600; i++) {
      // Irregular arrivals: a frame every ~0.4-1.6 s of wall time, ts jittered.
      if (i % (8 + (i % 5) * 4) === 0) {
        ts += 400 + ((i * 37) % 1200)
        h.push(snap(++tick, ts, [agent('a', tick, 0)]))
      }
      if (i === 100) h.setSpeed(2)
      if (i === 200) h.setSpeed(0.5)
      if (i === 300) h.goLive()
      if (i === 400) h.setSpeed(1)
      const v = h.advance(1 / 60)
      if (h.playback.mode === 'live' && !h.playback.easing) expect(v).toBeGreaterThanOrEqual(last)
      last = v
    }
  })

  it('produces continuous positions as the clock advances at 60 Hz', () => {
    const { h, out } = make()
    let ts = 0
    h.push(snap(1, ts, [agent('a', 0, 0)]))
    for (let i = 1; i < 40; i++) {
      ts += 400 + ((i * 53) % 900)
      h.push(snap(i + 1, ts, [agent('a', i * 1.5, Math.sin(i * 0.3) * 3)]))
    }
    h.scrubToIndex(0)
    h.setSpeed(1)
    const s = h.agentIndex.get('a')!
    h.sample(h.playback.renderVts, out)
    let lx = out.x[s]!
    let ly = out.y[s]!
    let maxJump = 0
    for (let i = 0; i < 2000; i++) {
      const v = h.advance(1 / 60)
      h.sample(v, out)
      const d = Math.hypot(out.x[s]! - lx, out.y[s]! - ly)
      if (d > maxJump) maxJump = d
      lx = out.x[s]!
      ly = out.y[s]!
    }
    // Max speed in the data is ~4.4 u/s → ≤ 0.075 u per 16.7 ms frame.
    expect(maxJump).toBeLessThan(0.1)
    expect(h.playback.renderVts).toBe(h.newestVts)
  })

  it('steps frame by frame and scrubs by index', () => {
    const { h } = make()
    feed(h, 6, 700)
    h.scrubToIndex(2)
    expect(h.renderTick()).toBe(3)
    h.step(1)
    expect(h.renderTick()).toBe(4)
    h.step(-1)
    h.step(-1)
    expect(h.renderTick()).toBe(2)
    expect(h.playback.speed).toBe(0)
    h.goLive()
    for (let i = 0; i < 60; i++) h.advance(1 / 60)
    expect(h.playback.easing).toBe(false)
    expect(h.playback.mode).toBe('live')
  })
})

describe('slots', () => {
  it('keeps slots stable across frames and grows capacity when needed', () => {
    const h = new History(16, 2, 1)
    const seen: number[] = []
    h.onCapacityChange((c) => seen.push(c))
    h.push(snap(1, 0, [agent('a', 0, 0), agent('b', 0, 0)]))
    const sa = h.agentIndex.get('a')!
    h.push(snap(2, 1000, [agent('a', 0, 0), agent('b', 0, 0), agent('c', 0, 0)]))
    expect(h.agentIndex.get('a')).toBe(sa)
    expect(h.capacity).toBeGreaterThanOrEqual(3)
    expect(seen.length).toBeGreaterThan(0)
    // Older frames keep their own capacity and are still sampleable.
    const out = new SampleOut(h.capacity, 4)
    h.sample(500, out)
    expect(out.present[h.agentIndex.get('c')!]).toBe(1)
    expect(out.count).toBe(h.capacity)
  })
})
