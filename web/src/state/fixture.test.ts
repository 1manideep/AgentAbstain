/**
 * Renderer-vs-mock (DESIGN §19 step 1): replays public/fixtures/mock.jsonl —
 * a genuine `void mock-feed` recording or the synthetic generator's output —
 * through the History buffer, the event ring and the series reducers, and
 * asserts the virtual timeline is monotone, sampling is continuous and every
 * message type is recognised. Skips when no fixture has been generated.
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { RosterAgent, ServerMsg, SnapshotMsg } from '../protocol'
import { EventRing } from './events'
import { CLAMP_FACTOR, History, NODE_FOOT, SampleOut } from './history'
import { Series } from './series'

const KNOWN = new Set(['hello', 'roster', 'snapshot', 'gadgets', 'tasks', 'chronicle', 'event', 'metrics', 'day', 'status'])
const path = resolve(import.meta.dirname, '..', '..', 'public', 'fixtures', 'mock.jsonl')

function load(): ServerMsg[] {
  return readFileSync(path, 'utf8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as ServerMsg)
}

describe.skipIf(!existsSync(path))('fixture replay', () => {
  const msgs = load()
  const snapshots = msgs.filter((m): m is SnapshotMsg => m.type === 'snapshot')

  it('contains only §15 message types, starting with hello', () => {
    expect(msgs[0]!.type).toBe('hello')
    for (const m of msgs) expect(KNOWN.has(m.type)).toBe(true)
    expect(snapshots.length).toBeGreaterThan(50)
  })

  it('builds a monotone virtual timeline with every step clamped to 3·ema', () => {
    const h = new History(600, 16, 8)
    let prevVts = -1
    let prevEma = h.ema
    for (const s of snapshots) {
      const f = h.push(s)
      expect(f.vts).toBeGreaterThanOrEqual(prevVts)
      if (prevVts >= 0) expect(f.vts - prevVts).toBeLessThanOrEqual(CLAMP_FACTOR * prevEma + 1e-6)
      prevVts = f.vts
      prevEma = h.ema
    }
    expect(h.count).toBe(Math.min(600, snapshots.length))
    // ticks are monotone too, so vtsOfTick resolves every tick that has a frame
    const last = snapshots[snapshots.length - 1]!
    expect(h.vtsOfTick(last.tick)).toBe(h.newestVts)
  })

  it('samples continuously across the whole buffer without NaN positions', () => {
    const h = new History(600, 16, 8)
    for (const s of snapshots) h.push(s)
    const out = new SampleOut(h.capacity, h.nodeCapacity)
    const lastX = new Float32Array(h.capacity).fill(Number.NaN)
    const lastY = new Float32Array(h.capacity).fill(Number.NaN)
    const lastId = new Array<string>(h.capacity).fill('')
    let maxJump = 0
    let presentSamples = 0
    for (let v = h.oldestVts; v <= h.newestVts; v += 16.7) {
      h.sample(v, out)
      for (let s = 0; s < out.count; s++) {
        if (!out.present[s]) {
          lastX[s] = Number.NaN
          lastId[s] = ''
          continue
        }
        presentSamples++
        expect(Number.isFinite(out.x[s])).toBe(true)
        expect(Number.isFinite(out.y[s])).toBe(true)
        expect(out.alpha[s]).toBeGreaterThanOrEqual(0)
        expect(out.scale[s]).toBeGreaterThanOrEqual(0)
        // a slot changing hands (a tombstone replaced by a newborn) is not a teleport: the newborn
        // scales in at its own position; continuity is asserted per id, not per slot
        if (Number.isFinite(lastX[s]!) && !out.dead[s] && lastId[s] === out.ids[s]) {
          const d = Math.hypot(out.x[s]! - lastX[s]!, out.y[s]! - lastY[s]!)
          if (d > maxJump) maxJump = d
        }
        lastX[s] = out.x[s]!
        lastY[s] = out.y[s]!
        lastId[s] = out.ids[s]!
      }
    }
    expect(presentSamples).toBeGreaterThan(0)
    // display spread: nobody STANDS inside a node crystal (closer than NODE_FOOT - 0.5 to its centre);
    // a walker may still cut across the stones on its way past, which the kernel's straight paths allow
    for (let v = h.oldestVts; v <= h.newestVts; v += 250) {
      h.sample(v, out)
      for (let s = 0; s < out.count; s++) {
        if (!out.present[s] || out.dead[s] || Math.hypot(out.vx[s]!, out.vy[s]!) > 0.3) continue
        for (let n = 0; n < out.nodeCount; n++) {
          if (!out.nodePresent[n]) continue
          expect(Math.hypot(out.x[s]! - out.nx[n]!, out.y[s]! - out.ny[n]!)).toBeGreaterThan(NODE_FOOT - 0.5)
        }
      }
    }
    // max_speed is 2 u/tick and the shortest virtual tick is ≥ 200 ms → < 0.2 u per 16.7 ms frame,
    // plus births appear at their position (scale 0) rather than jumping.
    expect(maxJump).toBeLessThan(0.5)
  })

  it('feeds every event and metrics row through the reducers idempotently', () => {
    const ring = new EventRing(2000)
    const series = new Series()
    const rosterById = new Map<string, RosterAgent>()
    let stored = 0
    for (const m of msgs) {
      if (m.type === 'roster') for (const a of m.agents) rosterById.set(a.id, a)
      if (m.type === 'event') {
        if (ring.push(m)) {
          stored++
          series.onEvent(m, rosterById)
        }
        // replaying the same seq is a no-op
        expect(ring.push(m)).toBe(false)
      }
      if (m.type === 'snapshot') series.onSnapshot(m, rosterById)
      if (m.type === 'metrics') series.onMetrics(m.row)
      if (m.type === 'day') series.onDay(m.row)
    }
    const events = msgs.filter((m) => m.type === 'event').length
    expect(stored).toBe(events)
    expect(ring.size).toBe(Math.min(2000, events))
    expect(series.popByTick.count).toBe(Math.min(720, snapshots.length))
    expect(series.metricsRows).toBe(msgs.filter((m) => m.type === 'metrics').length)
    expect(series.teffSamples).toBeGreaterThan(0)
  })
})
