import type { AnimationClip } from 'three'
import { Vector3 } from 'three'
import { describe, expect, it } from 'vitest'
import { BONE_NAMES, buildHumanoid, type BoneName } from './humanoid'
import { CLIP_LOOPS, CLIP_NAMES, buildClips } from './clips'
import { PRESETS, hashName, presetFor, presetIndexFor } from './presets'

const TEN = ['Ada', 'Bao', 'Cyra', 'Dev', 'Enzo', 'Faye', 'Gil', 'Hex', 'Ivo', 'Juno']

describe('presets', () => {
  it('has ten distinct presets keyed 0..9', () => {
    expect(PRESETS.length).toBe(10)
    PRESETS.forEach((p, i) => expect(p.id).toBe(i))
    const sigs = new Set(PRESETS.map((p) => JSON.stringify({ ...p, id: 0, name: '', description: '' })))
    expect(sigs.size).toBe(10)
    expect(new Set(PRESETS.map((p) => p.name)).size).toBe(10)
    expect(new Set(PRESETS.map((p) => p.skin)).size).toBeGreaterThanOrEqual(8)
    expect(new Set(PRESETS.map((p) => p.hair)).size).toBe(10)
    for (const p of PRESETS) {
      expect(p.height).toBeGreaterThanOrEqual(1.55)
      expect(p.height).toBeLessThanOrEqual(1.92)
      expect(p.description.length).toBeGreaterThan(10)
      expect(p.trim.length).toBeGreaterThan(0)
    }
  })

  it('maps the ten names by table, deterministically, case-insensitively', () => {
    TEN.forEach((n, i) => {
      expect(presetIndexFor(n)).toBe(i)
      expect(presetIndexFor(n.toUpperCase())).toBe(i)
      expect(presetFor(n)).toBe(PRESETS[i])
    })
    for (const n of ['Kai', 'Ada II', 'zephyr', 'ag_00000001ff7705']) {
      const a = presetIndexFor(n)
      expect(a).toBe(presetIndexFor(n))
      expect(a).toBeGreaterThanOrEqual(0)
      expect(a).toBeLessThan(10)
    }
    expect(hashName('kai')).toBe(hashName('kai'))
  })
})

describe('buildHumanoid', () => {
  it('yields the full bone hierarchy with skin indices in range and the preset height', () => {
    for (const preset of PRESETS) {
      const h = buildHumanoid(preset)
      expect(Object.keys(h.bones).sort()).toEqual([...BONE_NAMES].sort())
      expect(h.boneList.length).toBe(BONE_NAMES.length)
      expect(h.skeleton.bones.length).toBe(BONE_NAMES.length)
      for (const name of BONE_NAMES) expect(h.bones[name].name).toBe(name)
      // hierarchy: every bone but the hips has a bone parent, the hips hang off the root
      for (const name of BONE_NAMES) {
        const b = h.bones[name]
        if (name === 'hips') expect(b.parent).toBe(h.root)
        else expect(b.parent && (b.parent as { isBone?: boolean }).isBone).toBe(true)
      }
      expect(h.bones.forearmL.parent).toBe(h.bones.upperArmL)
      expect(h.bones.lowerLegR.parent).toBe(h.bones.upperLegR)
      expect(h.bones.head.parent).toBe(h.bones.neck)

      expect(h.height).toBeCloseTo(preset.height, 6)
      expect(h.skinned.length).toBe(2)
      let tris = 0
      // Every vertex of every mesh of every preset is checked; violations are collected and asserted
      // once per mesh, since a per-vertex expect() costs far more than the check itself.
      for (const [mi, m] of h.skinned.entries()) {
        const g = m.geometry
        const idx = g.getAttribute('skinIndex')
        const w = g.getAttribute('skinWeight')
        const pos = g.getAttribute('position')
        expect(idx.count).toBe(pos.count)
        expect(idx.count).toBeGreaterThan(0)
        const bad: string[] = []
        let badCount = 0
        for (let i = 0; i < idx.count; i++) {
          let sum = 0
          let inRange = true
          for (let k = 0; k < 4; k++) {
            const bi = idx.getComponent(i, k)
            if (!(bi >= 0 && bi < BONE_NAMES.length)) inRange = false
            sum += w.getComponent(i, k)
          }
          // toBeCloseTo(1, 4) passes when |sum - 1| < 0.5e-4
          if (!inRange || !(Math.abs(sum - 1) < 0.5e-4)) {
            badCount++
            if (bad.length < 20) {
              const bones = [0, 1, 2, 3].map((k) => idx.getComponent(i, k)).join(',')
              bad.push(`${preset.name} mesh ${mi} vertex ${i}: bones [${bones}] weight sum ${sum}`)
            }
          }
        }
        expect({ badCount, bad }).toEqual({ badCount: 0, bad: [] })
        tris += (g.index ? g.index.count : pos.count) / 3
      }
      expect(tris).toBeLessThanOrEqual(4000)
      // feet on the ground, skull at the preset height (hair may add a little)
      const bb = h.body.geometry.boundingBox!
      expect(bb.min.y).toBeGreaterThan(-0.02)
      expect(bb.min.y).toBeLessThan(0.02)
      expect(bb.max.y).toBeGreaterThanOrEqual(preset.height - 0.01)
      expect(bb.max.y).toBeLessThan(preset.height * 1.12)
      h.dispose()
    }
  })

  it('follows the figure guide: head ≈ 1/7.5, shoulders ≈ 2 heads, fingertips at mid-thigh', () => {
    const h = buildHumanoid(PRESETS[3]!)
    const u = h.dims.u
    expect(u * 7.5).toBeCloseTo(h.height, 6)
    expect(h.dims.shoulderX * 2).toBeCloseTo(2 * u * PRESETS[3]!.shoulder, 6)
    const wrist = h.dims.shoulderY - h.dims.upperArmLen - h.dims.forearmLen
    const fingertip = wrist - 0.6 * u
    const midThigh = (h.dims.hipJointY + (h.dims.hipJointY - h.dims.thighLen)) / 2
    expect(Math.abs(fingertip - midThigh)).toBeLessThan(0.35 * u)
    h.dispose()
  })
})

describe('buildClips', () => {
  it('references only existing bones and loops where stated', () => {
    for (const preset of [PRESETS[0]!, PRESETS[4]!, PRESETS[6]!]) {
      const h = buildHumanoid(preset)
      const clips = buildClips(h.bones, preset)
      expect(Object.keys(clips).sort()).toEqual([...CLIP_NAMES].sort())
      const names = new Set<string>(BONE_NAMES)
      for (const name of CLIP_NAMES) {
        const clip = clips[name]
        expect(clip.name).toBe(name)
        expect(clip.duration).toBeGreaterThan(0)
        expect(clip.tracks.length).toBeGreaterThan(0)
        for (const tr of clip.tracks) {
          const [node, prop] = tr.name.split('.')
          expect(names.has(node!)).toBe(true)
          expect(['quaternion', 'position']).toContain(prop)
          expect(tr.times.length).toBeGreaterThanOrEqual(2)
          expect(tr.times[0]).toBe(0)
          expect(tr.times[tr.times.length - 1]).toBeCloseTo(clip.duration, 5)
          const stride = tr.getValueSize()
          expect(tr.values.length).toBe(tr.times.length * stride)
          // every key value is checked; the non-finite ones are collected and asserted once per track
          const nonFinite: number[] = []
          for (let i = 0; i < tr.values.length; i++) if (!Number.isFinite(tr.values[i])) nonFinite.push(i)
          expect(nonFinite, `${name} ${tr.name}: non-finite values at`).toEqual([])
          if (CLIP_LOOPS[name]) {
            const n = tr.times.length
            for (let k = 0; k < stride; k++) expect(tr.values[k]).toBeCloseTo(tr.values[(n - 1) * stride + k]!, 6)
          }
        }
        // every bone is covered, so weighted blends never leak the bind pose
        const covered = new Set(clip.tracks.map((t) => t.name.split('.')[0]))
        for (const b of BONE_NAMES) expect(covered.has(b)).toBe(true)
      }
      // the walk actually moves the legs and bends knees the right way (positive x = heel back)
      const knee = clips.walk.tracks.find((t) => t.name === 'lowerLegL.quaternion')!
      let maxX = -1
      for (let i = 0; i < knee.times.length; i++) maxX = Math.max(maxX, knee.values[i * 4]!)
      expect(maxX).toBeGreaterThan(0.3)
      // dead ends on the ground: hips well below rest height at the last key
      const hp = clips.dead.tracks.find((t) => t.name === 'hips.position')!
      const last = hp.values.length - 3
      expect(hp.values[last + 1]!).toBeLessThan(h.bones.hips.position.y * 0.4)
      h.dispose()
    }
  })
})

describe('CharacterSystem module', () => {
  it('imports without touching the DOM', async () => {
    const mod = await import('./CharacterSystem')
    expect(typeof mod.CharacterSystem).toBe('function')
    expect(typeof mod.charactersEnabled).toBe('function')
  })
})

// ------------------------------------------------------------------ gestures, checked by forward kinematics
// Landmarks are in head units (u = height / 7.5); the character faces +Z with the hips at y ≈ 3.9 u.

function poseAt(h: ReturnType<typeof buildHumanoid>, clip: AnimationClip, t: number): void {
  const times = clip.tracks[0]!.times
  let k = 0
  for (let i = 1; i < times.length; i++) if (Math.abs(times[i]! - t) < Math.abs(times[k]! - t)) k = i
  for (const tr of clip.tracks) {
    const [node, prop] = tr.name.split('.') as [BoneName, string]
    const bone = h.bones[node]
    if (prop === 'quaternion') bone.quaternion.set(tr.values[k * 4]!, tr.values[k * 4 + 1]!, tr.values[k * 4 + 2]!, tr.values[k * 4 + 3]!)
    else bone.position.set(tr.values[k * 3]!, tr.values[k * 3 + 1]!, tr.values[k * 3 + 2]!)
  }
  h.root.updateMatrixWorld(true)
}

/** World position of a bone (optionally a point `along` u down its local +y axis), in head units. */
function at(h: ReturnType<typeof buildHumanoid>, bone: BoneName, along = 0): Vector3 {
  const v = new Vector3(0, along * h.dims.u, 0)
  h.bones[bone].localToWorld(v)
  return v.divideScalar(h.dims.u)
}

/** One quaternion component of a bone's track at key `k`. */
function qAt(clip: AnimationClip, bone: BoneName, k: number, comp: 0 | 1 | 2 | 3): number {
  const tr = clip.tracks.find((t) => t.name === `${bone}.quaternion`)!
  return tr.values[k * 4 + comp]!
}

describe('gesture clips', () => {
  const preset = PRESETS[3]!
  const h = buildHumanoid(preset)
  const clips = buildClips(h.bones, preset)
  const rest = (() => {
    poseAt(h, clips.idle, 0)
    return { hips: at(h, 'hips'), head: at(h, 'head'), handR: at(h, 'handR', -0.28), chest: at(h, 'chest') }
  })()

  it('one-shots start and end at the same pose so they blend in and out of idle', () => {
    for (const name of ['give', 'receive', 'summon', 'shake', 'cheer', 'slump', 'wave', 'talk'] as const) {
      const clip = clips[name]
      for (const tr of clip.tracks) {
        const n = tr.times.length
        const stride = tr.getValueSize()
        for (let k = 0; k < stride; k++) expect(tr.values[k]).toBeCloseTo(tr.values[(n - 1) * stride + k]!, 4)
      }
    }
  })

  it('think puts the right hand at the chin, in front of the chest', () => {
    poseAt(h, clips.think, 1.4)
    const hand = at(h, 'handR', -0.28)
    const head = at(h, 'head')
    const chest = at(h, 'chest')
    expect(Math.abs(hand.y - head.y)).toBeLessThan(0.9)
    expect(hand.z).toBeGreaterThan(chest.z + 0.4)
    expect(Math.abs(hand.x)).toBeLessThan(0.5)
  })

  it('write holds both hands at chest height in front while the head looks down and the pen hand moves', () => {
    poseAt(h, clips.write, 0.9)
    const l = at(h, 'handL', -0.28)
    const r = at(h, 'handR', -0.28)
    for (const hand of [l, r]) {
      expect(hand.y).toBeGreaterThan(rest.chest.y - 0.2)
      expect(hand.y).toBeLessThan(rest.head.y)
      expect(hand.z).toBeGreaterThan(1.0)
    }
    const clip = clips.write
    const n = clip.tracks[0]!.times.length
    let headDown = 0
    let handMin = Infinity
    let handMax = -Infinity
    for (let k = 0; k < n; k++) {
      headDown += qAt(clip, 'head', k, 0)
      const z = qAt(clip, 'handR', k, 2)
      handMin = Math.min(handMin, z)
      handMax = Math.max(handMax, z)
    }
    expect(headDown / n).toBeGreaterThan(0.05)
    expect(handMax - handMin).toBeGreaterThan(0.05)
  })

  it('give steps forward and extends both hands at waist height', () => {
    poseAt(h, clips.give, 0.7)
    expect(at(h, 'hips').z).toBeGreaterThan(rest.hips.z + 0.05)
    for (const side of ['handL', 'handR'] as const) {
      const hand = at(h, side, -0.28)
      expect(hand.z).toBeGreaterThan(1.5)
      expect(hand.y).toBeGreaterThan(rest.hips.y - 0.6)
      expect(hand.y).toBeLessThan(rest.chest.y)
    }
    // the feet stay planted while the hips move
    expect(Math.abs(at(h, 'footL').z)).toBeLessThan(0.1)
  })

  it('summon, cheer and wave raise hands above the head; cheer hops', () => {
    poseAt(h, clips.summon, 0.8)
    expect(at(h, 'handL', -0.28).y).toBeGreaterThan(rest.head.y + 1)
    expect(at(h, 'handR', -0.28).y).toBeGreaterThan(rest.head.y + 1)
    poseAt(h, clips.cheer, 0.55)
    expect(at(h, 'hips').y).toBeGreaterThan(rest.hips.y + 0.05)
    expect(at(h, 'footL').y).toBeGreaterThan(0.35)
    poseAt(h, clips.wave, 0.6)
    expect(at(h, 'handR', -0.28).y).toBeGreaterThan(rest.head.y + 1)
    expect(at(h, 'handL', -0.28).y).toBeLessThan(rest.hips.y)
  })

  it('idle_guard crosses the forearms and widens the stance', () => {
    poseAt(h, clips.idle_guard, 2)
    expect(at(h, 'handL', -0.28).x).toBeLessThan(0) // the left hand crosses to the right side
    expect(at(h, 'handR', -0.28).x).toBeGreaterThan(0)
    expect(at(h, 'footL').x).toBeGreaterThan(0.6)
  })

  it('slump hangs the head and lets the hands fall in front of the knees', () => {
    poseAt(h, clips.slump, 0.9)
    expect(at(h, 'head').z).toBeGreaterThan(0.6)
    const hand = at(h, 'handL', -0.28)
    expect(hand.z).toBeGreaterThan(at(h, 'lowerLegL').z)
    expect(hand.y).toBeLessThan(rest.hips.y)
  })

  it('tinker crouches with the hands working near knee height', () => {
    poseAt(h, clips.tinker, 0.9)
    expect(at(h, 'hips').y).toBeLessThan(rest.hips.y - 0.5)
    expect(at(h, 'handR', -0.28).y).toBeLessThan(rest.hips.y)
  })

  it('walk_brisk shares the walk leg phase so the two blend without foot sliding', () => {
    const sample = (clip: AnimationClip, phase: number) => {
      const tr = clip.tracks.find((t) => t.name === 'upperLegL.quaternion')!
      const n = tr.times.length
      const k = Math.round(phase * (n - 1))
      return tr.values[k * 4]!
    }
    for (const phase of [0.1, 0.25, 0.4, 0.6, 0.75, 0.9]) {
      const a = sample(clips.walk, phase)
      const b = sample(clips.walk_brisk, phase)
      expect(Math.sign(a)).toBe(Math.sign(b))
      expect(Math.abs(b)).toBeGreaterThanOrEqual(Math.abs(a) * 0.95)
    }
  })
})
