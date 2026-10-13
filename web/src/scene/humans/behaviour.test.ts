import { describe, expect, it } from 'vitest'
import type { EventMsg } from '../../protocol'
import {
  BEHAVIOUR_KINDS,
  BRISK_RATIO,
  FACE_LISTEN,
  FACE_TALK,
  GESTURE_KINDS,
  GestureQueue,
  HEAD_YAW_MAX,
  HOLD_MAINTENANCE,
  NO_GESTURE,
  PRIORITY_EXCHANGE,
  PRIORITY_HOLD,
  PRIORITY_URGENT,
  QUEUE_DEPTH,
  REDUCED_GESTURE_CAP,
  WALK_GESTURE_WEIGHT,
  applyGesture,
  cancelStandingHolds,
  envelope,
  facingYaw,
  finishCurrent,
  gestureFor,
  gestureScale,
  headYawFor,
  splitBase,
  styleFor,
  targetYawTo,
  walkTimeScale,
  wrapAngle,
  type Gesture,
} from './behaviour'
import { CLIP_DURATION, CLIP_NAMES } from './clipNames'

let seq = 0
function ev(kind: string, payload: Record<string, unknown>, agent_id: string | null = null, tick = 5): EventMsg {
  seq += 1
  return { type: 'event', seq, tick, day: 1, kind, agent_id, visibility: 'public', payload } as EventMsg
}

const g = (agentId: string, clip: Gesture['clip'], hold: number, priority: number, queued = false): Gesture => ({ agentId, clip, hold, priority, queued })

describe('gestureFor', () => {
  it('talk: the speaker talks facing the listener, the listener only turns to the speaker', () => {
    const out = gestureFor(ev('talk', { speaker_id: 'a', listener_id: 'b', text: 'hi' }, 'a'))
    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ agentId: 'a', clip: 'talk', faceId: 'b', faceFor: FACE_TALK, priority: PRIORITY_EXCHANGE })
    expect(out[0]!.hold).toBe(CLIP_DURATION.talk)
    expect(out[1]).toMatchObject({ agentId: 'b', clip: null, faceId: 'a', faceFor: FACE_LISTEN })
  })

  it('transfer and share_note: giver gives, receiver receives, each facing the other', () => {
    for (const [kind, p] of [['transfer', { from: 'a', to: 'b', amount_usd: 0.1 }], ['share_note', { speaker_id: 'a', listener_id: 'b' }]] as const) {
      const out = gestureFor(ev(kind, p))
      expect(out.map((x) => [x.agentId, x.clip, x.faceId])).toEqual([['a', 'give', 'b'], ['b', 'receive', 'a']])
    }
  })

  it('windfall: receive now, cheer queued after it', () => {
    const out = gestureFor(ev('windfall', { agent_id: 'a', amount_usd: 0.5 }))
    expect(out.map((x) => x.clip)).toEqual(['receive', 'cheer'])
    expect(out[1]!.queued).toBe(true)
  })

  it('holds, one-shots and failure reactions by kind', () => {
    const one = (kind: string, p: Record<string, unknown>, agent: string | null = 'a') => gestureFor(ev(kind, p, agent))
    expect(one('read_chronicle', { agent_id: 'a' })[0]).toMatchObject({ clip: 'read', hold: 3 })
    expect(one('nudge_weather', { agent_id: 'a', delta: 0.1 })[0]).toMatchObject({ clip: 'summon' })
    expect(one('propose_gadget', {})[0]).toMatchObject({ agentId: 'a', clip: 'tinker' })
    expect(one('use_gadget', {})[0]).toMatchObject({ clip: 'tinker' })
    expect(one('gadget_verified', { owner: 'o' }, null)[0]).toMatchObject({ agentId: 'o', clip: 'tinker' })
    expect(one('gadget_rejected', { owner: 'o' }, null)[0]).toMatchObject({ agentId: 'o', clip: 'slump', priority: PRIORITY_URGENT })
    expect(one('remember', { agent_id: 'a', title: 't' })[0]).toMatchObject({ clip: 'write', hold: 2, priority: PRIORITY_HOLD })
    expect(one('revise_self', { agent_id: 'a' })[0]).toMatchObject({ clip: 'write' })
    expect(one('maintenance', { agent_id: 'a', commands: 3 })[0]).toMatchObject({ clip: 'write', hold: HOLD_MAINTENANCE })
    expect(one('claim_feedback', { speaker_id: 's', listener_id: 'l' }, 'l')[0]).toMatchObject({ agentId: 'l', clip: 'shake', faceId: 's' })
    expect(one('gate_blocked', { agent_id: 'a', reason: 'insufficient' })[0]).toMatchObject({ clip: 'slump' })
    expect(one('action_failed', { agent_id: 'a', action: 'forage', kind: 'invalid' })[0]).toMatchObject({ clip: 'slump' })
    expect(one('action_failed', { agent_id: 'a', action: 'talk', kind: 'stale' })).toEqual([])
    expect(one('birth', { agent_id: 'c', name: 'Kid' }, 'c')[0]).toMatchObject({ agentId: 'c', clip: 'wave' })
    expect(one('task_completed', { agent_id: 'a' })[0]).toMatchObject({ clip: 'cheer' })
    expect(one('sleep', { agent_id: 'a' })).toEqual([])
    expect(one('weather', { value: 0.1 })).toEqual([])
  })

  it('every gesture clip it names exists, and the polled kinds cover the table plus forage', () => {
    for (const k of GESTURE_KINDS) expect(BEHAVIOUR_KINDS.has(k)).toBe(true)
    expect(BEHAVIOUR_KINDS.has('forage')).toBe(true)
    const kinds = ['talk', 'transfer', 'share_note', 'windfall', 'read_chronicle', 'nudge_weather', 'propose_gadget', 'gadget_rejected',
      'remember', 'maintenance', 'claim_feedback', 'gate_blocked', 'birth', 'task_assigned']
    for (const k of kinds) {
      for (const x of gestureFor(ev(k, { agent_id: 'a', speaker_id: 'a', listener_id: 'b', from: 'a', to: 'b', owner: 'a', kind: 'invalid' }, 'a'))) {
        if (x.clip !== null) expect(CLIP_NAMES).toContain(x.clip)
      }
    }
  })
})

describe('applyGesture and the queue', () => {
  it('starts, replaces at equal or higher priority, queues lower ones, keeps at most QUEUE_DEPTH', () => {
    let s = applyGesture(NO_GESTURE, g('a', 'write', 2, PRIORITY_HOLD), 0)
    expect(s.current?.clip).toBe('write')
    s = applyGesture(s, g('a', 'talk', 1.2, PRIORITY_EXCHANGE), 0.5)
    expect(s.current).toMatchObject({ clip: 'talk', start: 0.5 })
    s = applyGesture(s, g('a', 'read', 3, PRIORITY_HOLD), 0.6)
    s = applyGesture(s, g('a', 'tinker', 2, PRIORITY_HOLD), 0.7)
    s = applyGesture(s, g('a', 'summon', 1.6, PRIORITY_HOLD), 0.8)
    expect(s.current?.clip).toBe('talk')
    expect(s.queue.map((q) => q.clip)).toEqual(['tinker', 'summon'])
    expect(s.queue.length).toBeLessThanOrEqual(QUEUE_DEPTH)
    s = applyGesture(s, g('a', 'shake', 1, PRIORITY_URGENT), 0.9)
    expect(s.current?.clip).toBe('shake')
  })

  it('a repeated one-shot finishes what it started; a repeated hold is extended', () => {
    const s1 = applyGesture(NO_GESTURE, g('a', 'talk', 1.2, PRIORITY_EXCHANGE), 0)
    expect(applyGesture(s1, g('a', 'talk', 1.2, PRIORITY_EXCHANGE), 0.5)).toBe(s1)
    const s2 = applyGesture(NO_GESTURE, g('a', 'write', 2, PRIORITY_HOLD), 0)
    const s3 = applyGesture(s2, g('a', 'write', 2, PRIORITY_HOLD), 1.5)
    expect(s3.current).toMatchObject({ clip: 'write', start: 0, hold: 3.5 })
  })

  it('a face-only gesture changes nothing; a queued one waits even at higher priority', () => {
    const s = applyGesture(NO_GESTURE, g('a', 'receive', 1.2, PRIORITY_EXCHANGE), 0)
    expect(applyGesture(s, g('a', null, 0, 0), 0.1)).toBe(s)
    const q = applyGesture(s, g('a', 'cheer', 1.2, PRIORITY_URGENT, true), 0.1)
    expect(q.current?.clip).toBe('receive')
    expect(q.queue.map((x) => x.clip)).toEqual(['cheer'])
    const next = finishCurrent(q, 1.3)
    expect(next.current).toMatchObject({ clip: 'cheer', start: 1.3 })
    expect(finishCurrent(next, 2.5)).toBe(NO_GESTURE)
    expect(finishCurrent(NO_GESTURE, 3)).toBe(NO_GESTURE)
  })

  it('walking cancels standing holds but keeps one-shots', () => {
    let s = applyGesture(NO_GESTURE, g('a', 'write', 2, PRIORITY_HOLD), 0)
    s = applyGesture(s, g('a', 'read', 3, PRIORITY_HOLD, true), 0)
    s = applyGesture(s, g('a', 'wave', 1.2, PRIORITY_HOLD, true), 0)
    const c = cancelStandingHolds(s, 1)
    expect(c.current).toMatchObject({ clip: 'wave', start: 1 })
    expect(c.queue).toHaveLength(0)
    const t = applyGesture(NO_GESTURE, g('a', 'talk', 1.2, PRIORITY_EXCHANGE), 0)
    expect(cancelStandingHolds(t, 1)).toBe(t)
    expect(cancelStandingHolds(applyGesture(NO_GESTURE, g('a', 'tinker', 2, PRIORITY_HOLD), 0), 1)).toBe(NO_GESTURE)
  })
})

describe('envelopes and weights', () => {
  it('is a trapezoid that is zero outside and one in the middle', () => {
    expect(envelope(0, 2, 0.25, 0.35)).toBe(0)
    expect(envelope(2, 2, 0.25, 0.35)).toBe(0)
    expect(envelope(1, 2, 0.25, 0.35)).toBe(1)
    expect(envelope(0.125, 2, 0.25, 0.35)).toBeCloseTo(0.5)
    expect(envelope(-1, 2, 0.25, 0.35)).toBe(0)
  })

  it('scales gestures down while walking and caps them under reduced motion', () => {
    expect(gestureScale(0, false)).toBe(1)
    expect(gestureScale(1, false)).toBeCloseTo(WALK_GESTURE_WEIGHT)
    expect(gestureScale(0, true)).toBe(REDUCED_GESTURE_CAP)
    expect(gestureScale(5, false)).toBeCloseTo(WALK_GESTURE_WEIGHT)
  })
})

describe('facing and glances', () => {
  it('wraps angles into (-pi, pi] and turns along the shortest arc across the seam', () => {
    expect(wrapAngle(3 * Math.PI)).toBeCloseTo(Math.PI)
    expect(wrapAngle(-Math.PI)).toBeCloseTo(Math.PI)
    const from = Math.PI - 0.1
    const to = -Math.PI + 0.1
    const step = facingYaw(from, to, 0.05, 6)
    // shortest arc goes up through pi, not down through zero
    expect(Math.abs(wrapAngle(step - from))).toBeLessThan(0.2)
    expect(wrapAngle(step - from)).toBeGreaterThan(0)
    let y = 0
    for (let i = 0; i < 200; i++) y = facingYaw(y, 1, 1 / 60, 6)
    expect(y).toBeCloseTo(1, 3)
    expect(facingYaw(0.3, 0.3, 0.1, 6)).toBeCloseTo(0.3)
  })

  it('targetYawTo matches the renderer convention (heading 0 = +x; root yaw = pi/2 - heading)', () => {
    const h = targetYawTo(0, 0, 1, 0)
    expect(h).toBeCloseTo(0)
    expect(Math.PI / 2 - h).toBeCloseTo(Math.PI / 2)
    expect(targetYawTo(0, 0, 0, 1)).toBeCloseTo(Math.PI / 2)
    // the body forward for heading h is (cos h, sin h): it points at the target
    const t = targetYawTo(2, 3, -1, 7)
    expect(Math.cos(t)).toBeCloseTo(-3 / 5)
    expect(Math.sin(t)).toBeCloseTo(4 / 5)
    expect(Number.isFinite(targetYawTo(1, 1, 1, 1))).toBe(true)
  })

  it('headYawFor is zero straight ahead, signed by side and clamped', () => {
    expect(headYawFor(0, 0, 0, 5, 0)).toBeCloseTo(0)
    const left = headYawFor(0, 0, 0, 5, -2)
    const right = headYawFor(0, 0, 0, 5, 2)
    expect(left).toBeGreaterThan(0)
    expect(right).toBeLessThan(0)
    expect(headYawFor(0, 0, 0, -5, 0.01)).toBeCloseTo(-HEAD_YAW_MAX)
    expect(Math.abs(headYawFor(0, 0, 0, -5, -0.01))).toBeCloseTo(HEAD_YAW_MAX)
  })
})

describe('personality style', () => {
  it('follows the documented formulas at the extremes', () => {
    const zero = styleFor({ greed: 0, sociability: 0, curiosity: 0, caution: 0, industriousness: 0, honesty: 0 }, 0)
    expect(zero).toMatchObject({ guard: 0, brisk: 0, idleTimeScale: 0.85, walkTimeScale: 0.9, leanForward: 0 })
    expect(zero.fidget).toBeCloseTo(0.1)
    const one = styleFor({ greed: 1, sociability: 1, curiosity: 1, caution: 1, industriousness: 1, honesty: 1 }, 0)
    expect(one.fidget).toBeCloseTo(0.2)
    expect(one.guard).toBeCloseTo(0.8)
    expect(one.brisk).toBeCloseTo(0.95)
    expect(one.idleTimeScale).toBeCloseTo(1.2)
    expect(one.walkTimeScale).toBeCloseTo(1.15)
    const stressed = styleFor({ curiosity: 1, caution: 0 }, 1)
    expect(stressed.fidget).toBe(1)
    const defaults = styleFor(undefined, 0)
    expect(defaults.fidget).toBeCloseTo(0.15)
    expect(defaults.guard).toBeCloseTo(0.25)
    expect(styleFor({ curiosity: Number.NaN }, Number.NaN).idleTimeScale).toBeCloseTo(0.85 + 0.35 * 0.5)
  })

  it('splits the idle and walk weights without changing their totals', () => {
    for (const traits of [undefined, { curiosity: 1, caution: 0 }, { caution: 1, greed: 1 }, { industriousness: 1 }]) {
      for (const stress of [0, 0.5, 1]) {
        const s = splitBase(0.8, 0.3, styleFor(traits, stress))
        expect(s.idle + s.fidget + s.guard).toBeCloseTo(0.8)
        expect(s.walk + s.brisk).toBeCloseTo(0.3)
        for (const v of Object.values(s)) expect(v).toBeGreaterThanOrEqual(0)
      }
    }
    const both = splitBase(1, 0, { fidget: 0.9, guard: 0.9, brisk: 0, idleTimeScale: 1, walkTimeScale: 1, leanForward: 0 })
    expect(both.idle).toBe(0)
    expect(both.fidget).toBeCloseTo(0.5)
  })

  it('keeps the walk time scale in bounds and the brisk ratio below one', () => {
    const st = styleFor(undefined, 0)
    expect(walkTimeScale(0, st)).toBeCloseTo(0.55 * st.walkTimeScale)
    expect(walkTimeScale(100, st)).toBeCloseTo(1.9 * st.walkTimeScale)
    expect(BRISK_RATIO).toBeLessThan(1)
  })
})

describe('GestureQueue', () => {
  it('fires accepted kinds in tick order at the render tick, drops stale ones, dedups by seq and reseeds', () => {
    const q = new GestureQueue()
    const a = ev('remember', { agent_id: 'a' }, 'a', 10)
    const b = ev('talk', { speaker_id: 'a', listener_id: 'b' }, 'a', 8)
    const c = ev('weather', {}, null, 9)
    const old = ev('forage', { agent_id: 'a' }, 'a', 1)
    q.push(a)
    q.push(b)
    q.push(c)
    q.push(a)
    q.push(old)
    expect(q.size).toBe(3)
    const fired: string[] = []
    q.drain(9, (e) => fired.push(`${e.kind}@${e.tick}`))
    expect(fired).toEqual(['talk@8'])
    q.drain(10, (e) => fired.push(`${e.kind}@${e.tick}`))
    expect(fired).toEqual(['talk@8', 'remember@10'])
    q.reseed([a, b, old], 8)
    expect(q.size).toBe(1)
    q.clear()
    expect(q.size).toBe(0)
  })
})
