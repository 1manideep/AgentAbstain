import { describe, expect, it } from 'vitest'
import { BONE_NAMES, buildHumanoid } from './humanoid'
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
      for (const m of h.skinned) {
        const g = m.geometry
        const idx = g.getAttribute('skinIndex')
        const w = g.getAttribute('skinWeight')
        const pos = g.getAttribute('position')
        expect(idx.count).toBe(pos.count)
        for (let i = 0; i < idx.count; i++) {
          let sum = 0
          for (let k = 0; k < 4; k++) {
            const bi = idx.getComponent(i, k)
            expect(bi).toBeGreaterThanOrEqual(0)
            expect(bi).toBeLessThan(BONE_NAMES.length)
            sum += w.getComponent(i, k)
          }
          expect(sum).toBeCloseTo(1, 4)
        }
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
          for (let i = 0; i < tr.values.length; i++) expect(Number.isFinite(tr.values[i])).toBe(true)
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
