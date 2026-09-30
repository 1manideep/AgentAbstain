/**
 * Code-authored animation clips for the procedural humanoid. Every clip is a
 * full-body pose sequence (a quaternion track per bone plus a hips position
 * track), so blending several clips by weight never leaks the bind pose in.
 *
 * Conventions (bones share the world axes at rest, character faces +Z):
 *   upper arm / leg  rotation.x < 0 → swings forward, > 0 → backward
 *   lower leg        rotation.x > 0 → knee bends (heel goes back)
 *   forearm          rotation.x < 0 → elbow bends (hand comes forward/up)
 *   upperArmL z > 0 / upperArmR z < 0 → arm raised sideways
 *   spine / head     rotation.x > 0 → tilts forward
 */
import { AnimationClip, Bone, Euler, Quaternion, QuaternionKeyframeTrack, VectorKeyframeTrack } from 'three'
import { BONE_INDEX, BONE_NAMES, type BoneName } from './humanoid'
import type { Preset } from './presets'

export type ClipName = 'idle' | 'walk' | 'sleep' | 'degenerate' | 'dead' | 'talk' | 'forage'
export const CLIP_NAMES: readonly ClipName[] = ['idle', 'walk', 'sleep', 'degenerate', 'dead', 'talk', 'forage']
/** Whether the clip loops (first and last keyframes are equal) or clamps at its end. */
export const CLIP_LOOPS: Readonly<Record<ClipName, boolean>> = {
  idle: true,
  walk: true,
  sleep: true,
  degenerate: true,
  dead: false,
  talk: false,
  forage: true,
}
export const CLIP_DURATION: Readonly<Record<ClipName, number>> = {
  idle: 6,
  walk: 1,
  sleep: 4,
  degenerate: 1.6,
  dead: 1.4,
  talk: 1.2,
  forage: 1.5,
}

const TAU = Math.PI * 2
const N = BONE_NAMES.length
const B = BONE_INDEX

/** One full-body pose: Euler XYZ per bone plus a hips position offset. */
class Pose {
  rot = new Float64Array(N * 3)
  /** Hips position offset from rest (units). */
  px = 0
  py = 0
  pz = 0
  reset(): void {
    this.rot.fill(0)
    this.px = this.py = this.pz = 0
  }
  set(b: number, x: number, y: number, z: number): void {
    this.rot[b * 3] = x
    this.rot[b * 3 + 1] = y
    this.rot[b * 3 + 2] = z
  }
  add(b: number, x: number, y: number, z: number): void {
    this.rot[b * 3] += x
    this.rot[b * 3 + 1] += y
    this.rot[b * 3 + 2] += z
  }
  copy(o: Pose): void {
    this.rot.set(o.rot)
    this.px = o.px
    this.py = o.py
    this.pz = o.pz
  }
  lerp(a: Pose, b: Pose, t: number): void {
    for (let i = 0; i < this.rot.length; i++) this.rot[i] = a.rot[i]! + (b.rot[i]! - a.rot[i]!) * t
    this.px = a.px + (b.px - a.px) * t
    this.py = a.py + (b.py - a.py) * t
    this.pz = a.pz + (b.pz - a.pz) * t
  }
}

interface Ctx {
  u: number
  hipsRest: [number, number, number]
  thigh: number
  shin: number
}

/** Smooth 0→1→0 hill between a and b. */
function bump(t: number, a: number, b: number): number {
  if (t <= a || t >= b) return 0
  const x = (t - a) / (b - a)
  return 0.5 - 0.5 * Math.cos(TAU * x)
}

function smooth(e0: number, e1: number, x: number): number {
  const t = x <= e0 ? 0 : x >= e1 ? 1 : (x - e0) / (e1 - e0)
  return t * t * (3 - 2 * t)
}

function hash(n: number): number {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453
  return x - Math.floor(x)
}

/** Hips offset that keeps the ankles planted for a thigh angle `a` and knee bend `k` (both legs). */
function plant(c: Ctx, a: number, k: number): [dy: number, dz: number] {
  const dy = c.thigh * (Math.cos(a) - 1) + c.shin * (Math.cos(a + k) - 1)
  const dz = c.thigh * Math.sin(a) + c.shin * Math.sin(a + k)
  return [dy, dz]
}

/** Relaxed standing pose: arms slightly away from the body, elbows soft. */
function rest(p: Pose): void {
  p.reset()
  p.set(B.upperArmL, 0.05, 0, 0.1)
  p.set(B.upperArmR, 0.05, 0, -0.1)
  p.set(B.forearmL, -0.16, 0.05, 0.03)
  p.set(B.forearmR, -0.16, -0.05, -0.03)
  p.set(B.handL, 0, 0, 0.05)
  p.set(B.handR, 0, 0, -0.05)
  p.set(B.upperLegL, 0, 0, 0.02)
  p.set(B.upperLegR, 0, 0, -0.02)
}

// ------------------------------------------------------------------ poses

function idlePose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const br = Math.sin((TAU * t) / 3)
  p.add(B.chest, 0.035 * br, 0, 0)
  p.add(B.neck, -0.02 * br, 0, 0)
  p.add(B.shoulderL, 0, 0, 0.035 * br)
  p.add(B.shoulderR, 0, 0, -0.035 * br)
  p.add(B.upperArmL, 0, 0, 0.02 * br)
  p.add(B.upperArmR, 0, 0, -0.02 * br)
  const sway = Math.sin((TAU * t) / 6)
  p.add(B.hips, 0, 0, 0.03 * sway)
  p.add(B.spine, 0.02, 0, -0.025 * sway)
  p.add(B.chest, 0, 0.03 * sway, 0)
  p.add(B.upperLegL, 0, 0, -0.03 * sway)
  p.add(B.upperLegR, 0, 0, -0.03 * sway)
  p.px = 0.03 * c.u * sway
  p.py = 0.008 * c.u * br
  const turn = bump(t, 2.2, 3.6)
  p.add(B.head, 0.02, 0.45 * turn, 0.03 * turn)
  p.add(B.neck, 0, 0.15 * turn, 0)
  const turn2 = bump(t, 4.6, 5.6)
  p.add(B.head, 0.04 * turn2, -0.25 * turn2, -0.03 * turn2)
}

function walkPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const phi = TAU * t
  const leg = (ph: number, ul: number, ll: number, ft: number, sgn: number) => {
    const a = -0.55 * Math.sin(ph)
    const c0 = Math.max(0, Math.cos(ph))
    const k = 0.12 + 1.05 * Math.pow(c0, 1.6)
    const push = Math.max(0, -Math.sin(ph - 0.5))
    p.set(ul, a, 0, sgn * 0.03)
    p.set(ll, k, 0, 0)
    p.set(ft, -(a + k) * 0.55 + 0.35 * push * push, 0, 0)
  }
  leg(phi, B.upperLegL, B.lowerLegL, B.footL, 1)
  leg(phi + Math.PI, B.upperLegR, B.lowerLegR, B.footR, -1)
  const arm = (ph: number, ua: number, fa: number, sgn: number) => {
    const s = Math.sin(ph)
    p.set(ua, 0.5 * s, 0, sgn * 0.12)
    p.set(fa, -0.25 - 0.4 * Math.max(0, -s), sgn * 0.08, sgn * 0.04)
  }
  arm(phi, B.upperArmL, B.forearmL, 1)
  arm(phi + Math.PI, B.upperArmR, B.forearmR, -1)
  const s = Math.sin(phi)
  p.set(B.hips, 0.02, -0.12 * s, 0.06 * s)
  p.set(B.spine, 0.06, 0.08 * s, -0.03 * s)
  p.set(B.chest, 0.03, 0.07 * s, -0.02 * s)
  p.set(B.neck, -0.03, -0.03 * s, 0)
  p.set(B.head, -0.05, 0, 0)
  p.py = 0.12 * c.u * Math.cos(2 * phi)
}

function sleepPose(t: number, p: Pose, c: Ctx): void {
  p.reset()
  const br = Math.sin((TAU * t) / 4)
  p.set(B.hips, 0.12, -0.2, Math.PI / 2)
  p.set(B.spine, 0.42, 0, 0)
  p.set(B.chest, 0.22 + 0.04 * br, 0, 0)
  p.set(B.neck, 0.12, 0, 0)
  p.set(B.head, 0.2, 0.1, 0)
  p.set(B.upperLegL, -1.35, 0, 0.06)
  p.set(B.upperLegR, -1.2, 0, -0.06)
  p.set(B.lowerLegL, 1.5, 0, 0)
  p.set(B.lowerLegR, 1.35, 0, 0)
  p.set(B.footL, 0.3, 0, 0)
  p.set(B.footR, 0.3, 0, 0)
  p.set(B.upperArmL, -1.15, 0, 0.3)
  p.set(B.upperArmR, -1.05, 0, -0.2)
  p.set(B.forearmL, -1.6, 0.4, 0)
  p.set(B.forearmR, -1.7, -0.3, 0)
  p.set(B.handL, -0.3, 0, 0)
  p.set(B.handR, -0.3, 0, 0)
  p.px = 0
  p.py = 0.86 * c.u - c.hipsRest[1] + 0.012 * c.u * br
  p.pz = 0
}

function degeneratePose(k: number, p: Pose, _c: Ctx): void {
  rest(p)
  p.set(B.upperArmL, -0.3, 0, 1.25)
  p.set(B.upperArmR, -0.3, 0, -1.25)
  p.set(B.forearmL, -0.5, 0, 0.3)
  p.set(B.forearmR, -0.5, 0, -0.3)
  p.set(B.upperLegL, -0.25, 0, 0.08)
  p.set(B.upperLegR, -0.25, 0, -0.08)
  p.set(B.lowerLegL, 0.45, 0, 0)
  p.set(B.lowerLegR, 0.45, 0, 0)
  p.set(B.footL, -0.2, 0, 0)
  p.set(B.footR, -0.2, 0, 0)
  p.set(B.spine, 0.05, 0, 0.1)
  const j = (b: number, ax: number, ay: number, az: number) => {
    p.add(b, ax * (hash(k * 31 + b * 7 + 1) - 0.5) * 2, ay * (hash(k * 31 + b * 7 + 2) - 0.5) * 2, az * (hash(k * 31 + b * 7 + 3) - 0.5) * 2)
  }
  j(B.head, 0.3, 0.55, 0.3)
  j(B.neck, 0.1, 0.15, 0.1)
  j(B.chest, 0.08, 0.12, 0.08)
  j(B.spine, 0.1, 0.1, 0.12)
  j(B.hips, 0.06, 0.1, 0.06)
  j(B.upperArmL, 0.35, 0.2, 0.3)
  j(B.upperArmR, 0.35, 0.2, 0.3)
  j(B.forearmL, 0.45, 0.2, 0.2)
  j(B.forearmR, 0.45, 0.2, 0.2)
  j(B.handL, 0.5, 0.3, 0.5)
  j(B.handR, 0.5, 0.3, 0.5)
  j(B.upperLegL, 0.1, 0.05, 0.06)
  j(B.upperLegR, 0.1, 0.05, 0.06)
  j(B.lowerLegL, 0.12, 0, 0)
  j(B.lowerLegR, 0.12, 0, 0)
  p.py = 0.06 * _c.u * (hash(k * 31 + 99) - 0.5)
}

function talkPose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const e = smooth(0, 0.25, t) * (1 - smooth(0.92, 1.2, t))
  p.set(B.upperArmR, 0.05 - 1.0 * e, 0.1 * e, -0.1 - 0.35 * e)
  p.set(B.forearmR, -0.16 - 1.5 * e, -0.05 - 0.3 * e, -0.03)
  p.set(B.handR, -0.1 * e, 0, -0.05 + 0.45 * Math.sin(TAU * 3.3 * t) * e)
  p.set(B.upperArmL, 0.05 - 0.35 * e, 0, 0.1 + 0.2 * e)
  p.set(B.forearmL, -0.16 - 0.7 * e, 0.05 + 0.3 * e, 0.03)
  p.add(B.head, 0.12 * Math.sin(TAU * 2.5 * t) * e, 0.12 * e, 0)
  p.add(B.neck, 0.05 * e, 0.05 * e, 0)
  p.add(B.chest, 0.03 * e, -0.08 * e, 0)
  p.add(B.spine, 0.03 * e, 0, 0)
}

function foragePose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const reach = 0.5 - 0.5 * Math.cos((TAU * t) / 1.5)
  const a = -1.0 - 0.12 * reach
  const k = 1.3 + 0.15 * reach
  const [dy, dz] = plant(c, a, k)
  for (const side of ['L', 'R'] as const) {
    const sgn = side === 'L' ? 1 : -1
    p.set(B[`upperLeg${side}`], a, sgn * 0.12, sgn * 0.14)
    p.set(B[`lowerLeg${side}`], k, 0, 0)
    p.set(B[`foot${side}`], -(a + k), 0, 0)
  }
  p.set(B.hips, 0.1, -0.1 * reach, 0)
  p.set(B.spine, 0.55 + 0.15 * reach, 0, 0)
  p.set(B.chest, 0.22, -0.05 * reach, 0)
  p.set(B.neck, 0.15, 0, 0)
  p.set(B.head, 0.3, 0.08 * reach, 0)
  p.set(B.upperArmR, -1.15 - 0.35 * reach, 0.1, -0.2)
  p.set(B.forearmR, -0.15 - 0.55 * reach, 0, -0.05)
  p.set(B.handR, -0.3 * reach, 0, 0)
  p.set(B.upperArmL, -0.9, 0, 0.25)
  p.set(B.forearmL, -0.5, 0.1, 0.05)
  p.py = dy - 0.15 * c.u * reach
  p.pz = dz
}

/** Dead: knees buckle, falls onto the back, settles. Keyed, clamps at the end. */
function deadKeys(c: Ctx): Array<{ t: number; pose: Pose }> {
  const keys: Array<{ t: number; pose: Pose }> = []
  const k = (t: number, fill: (p: Pose) => void) => {
    const p = new Pose()
    fill(p)
    keys.push({ t, pose: p })
  }
  k(0, (p) => rest(p))
  k(0.3, (p) => {
    rest(p)
    const [dy, dz] = plant(c, -0.5, 1.1)
    p.set(B.upperLegL, -0.5, 0, 0.05)
    p.set(B.upperLegR, -0.5, 0, -0.05)
    p.set(B.lowerLegL, 1.1, 0, 0)
    p.set(B.lowerLegR, 1.1, 0, 0)
    p.set(B.footL, -0.6, 0, 0)
    p.set(B.footR, -0.6, 0, 0)
    p.set(B.spine, 0.35, 0, 0)
    p.set(B.chest, 0.15, 0, 0)
    p.set(B.head, 0.3, 0, 0)
    p.set(B.upperArmL, -0.3, 0, 0.2)
    p.set(B.upperArmR, -0.3, 0, -0.2)
    p.set(B.forearmL, -0.6, 0, 0)
    p.set(B.forearmR, -0.6, 0, 0)
    p.py = dy
    p.pz = dz
  })
  k(0.7, (p) => {
    rest(p)
    p.set(B.hips, -1.1, 0, 0)
    p.set(B.upperLegL, -0.2, 0, 0.1)
    p.set(B.upperLegR, -0.1, 0, -0.1)
    p.set(B.lowerLegL, 0.5, 0, 0)
    p.set(B.lowerLegR, 0.3, 0, 0)
    p.set(B.spine, 0.1, 0, 0)
    p.set(B.upperArmL, -0.2, 0, 0.8)
    p.set(B.upperArmR, -0.2, 0, -0.8)
    p.set(B.forearmL, -0.3, 0, 0)
    p.set(B.forearmR, -0.3, 0, 0)
    p.set(B.head, -0.3, 0, 0)
    p.py = 0.62 * c.u - c.hipsRest[1]
    p.pz = -0.25 * c.u
  })
  const ground = (p: Pose, y: number) => {
    p.reset()
    p.set(B.hips, -Math.PI / 2, 0, 0)
    p.set(B.upperLegL, -0.35, 0, 0.12)
    p.set(B.upperLegR, -0.1, 0, -0.15)
    p.set(B.lowerLegL, 0.9, 0, 0)
    p.set(B.lowerLegR, 0.15, 0, 0)
    p.set(B.footL, 0.2, 0, 0)
    p.set(B.footR, 0.4, 0, 0)
    p.set(B.spine, -0.05, 0, 0)
    p.set(B.upperArmL, -0.15, 0, 1.0)
    p.set(B.upperArmR, -0.15, 0, -1.1)
    p.set(B.forearmL, -0.3, 0, 0.2)
    p.set(B.forearmR, -0.5, 0, -0.1)
    p.set(B.neck, -0.15, 0, 0)
    p.set(B.head, -0.25, 0.5, 0)
    p.py = y - c.hipsRest[1]
    p.pz = -0.45 * c.u
  }
  k(1.0, (p) => ground(p, 0.36 * c.u))
  k(1.12, (p) => ground(p, 0.43 * c.u))
  k(1.4, (p) => ground(p, 0.36 * c.u))
  return keys
}

// ------------------------------------------------------------------ tracks

function tracksFrom(times: number[], poses: Pose[], c: Ctx): Array<QuaternionKeyframeTrack | VectorKeyframeTrack> {
  const n = times.length
  const e = new Euler()
  const q = new Quaternion()
  const tracks: Array<QuaternionKeyframeTrack | VectorKeyframeTrack> = []
  for (let b = 0; b < N; b++) {
    const vals = new Float32Array(n * 4)
    for (let i = 0; i < n; i++) {
      const r = poses[i]!.rot
      e.set(r[b * 3]!, r[b * 3 + 1]!, r[b * 3 + 2]!, 'XYZ')
      q.setFromEuler(e)
      vals[i * 4] = q.x
      vals[i * 4 + 1] = q.y
      vals[i * 4 + 2] = q.z
      vals[i * 4 + 3] = q.w
    }
    tracks.push(new QuaternionKeyframeTrack(`${BONE_NAMES[b]}.quaternion`, times, Array.from(vals)))
  }
  const pv = new Float32Array(n * 3)
  for (let i = 0; i < n; i++) {
    const p = poses[i]!
    pv[i * 3] = c.hipsRest[0] + p.px
    pv[i * 3 + 1] = c.hipsRest[1] + p.py
    pv[i * 3 + 2] = c.hipsRest[2] + p.pz
  }
  tracks.push(new VectorKeyframeTrack('hips.position', times, Array.from(pv)))
  return tracks
}

function sampled(name: ClipName, duration: number, fps: number, fn: (t: number, p: Pose, c: Ctx) => void, c: Ctx, loop: boolean): AnimationClip {
  const n = Math.max(2, Math.round(duration * fps)) + 1
  const times: number[] = []
  const poses: Pose[] = []
  for (let i = 0; i < n; i++) {
    const t = (i / (n - 1)) * duration
    const p = new Pose()
    fn(t, p, c)
    times.push(t)
    poses.push(p)
  }
  if (loop) poses[n - 1]!.copy(poses[0]!)
  return new AnimationClip(name, duration, tracksFrom(times, poses, c))
}

function stepped(name: ClipName, duration: number, steps: number, fn: (k: number, p: Pose, c: Ctx) => void, c: Ctx): AnimationClip {
  const times: number[] = []
  const poses: Pose[] = []
  for (let k = 0; k <= steps; k++) {
    const p = new Pose()
    fn(k === steps ? 0 : k, p, c)
    times.push((k / steps) * duration)
    poses.push(p)
  }
  return new AnimationClip(name, duration, tracksFrom(times, poses, c))
}

function keyed(name: ClipName, keys: Array<{ t: number; pose: Pose }>, c: Ctx): AnimationClip {
  const times = keys.map((k) => k.t)
  const poses = keys.map((k) => k.pose)
  return new AnimationClip(name, times[times.length - 1]!, tracksFrom(times, poses, c))
}

/**
 * Build the seven clips for a skeleton. `bones` gives the rest hips position
 * (position tracks are absolute); `preset` gives the head unit and limb lengths.
 */
export function buildClips(bones: Record<BoneName, Bone>, preset: Preset): Record<ClipName, AnimationClip> {
  const u = preset.height / 7.5
  const h = bones.hips.position
  const c: Ctx = { u, hipsRest: [h.x, h.y, h.z], thigh: 1.75 * u, shin: 1.7 * u }
  return {
    idle: sampled('idle', CLIP_DURATION.idle, 8, idlePose, c, true),
    walk: sampled('walk', CLIP_DURATION.walk, 24, walkPose, c, true),
    sleep: sampled('sleep', CLIP_DURATION.sleep, 8, sleepPose, c, true),
    degenerate: stepped('degenerate', CLIP_DURATION.degenerate, 20, degeneratePose, c),
    dead: keyed('dead', deadKeys(c), c),
    talk: sampled('talk', CLIP_DURATION.talk, 20, talkPose, c, false),
    forage: sampled('forage', CLIP_DURATION.forage, 16, foragePose, c, true),
  }
}
