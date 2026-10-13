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
 *   upper arm y      twist about the arm: turns the plane the elbow bends in
 *                    (L y < 0 / R y > 0 swings the forearm across the body)
 *   hand y           twist about the forearm (±1.3 turns the palm up)
 *   spine / head     rotation.x > 0 → tilts forward
 *
 * Authoring rules: standing poses start from rest(); legs that touch the ground
 * go through plant() / legsPlanted() so the ankles stay put; one-shots start AND
 * end exactly at rest() (their envelopes are smooth()/bump() products that are 0
 * at both ends) so they blend in and out of idle without a pop; loops close
 * (sampled() copies the first key over the last) and use whole cycles of every
 * periodic term. Gaits share one leg-phase function so walk and walk_brisk
 * blend without foot-slide. Arm targets (chin, chest, crossed arms) were tuned
 * by forward kinematics on the built skeleton; the tests check them that way.
 */
import { AnimationClip, Bone, Euler, Quaternion, QuaternionKeyframeTrack, VectorKeyframeTrack } from 'three'
import { BONE_INDEX, BONE_NAMES, type BoneName } from './humanoid'
import type { Preset } from './presets'

export { CLIP_DURATION, CLIP_LOOPS, CLIP_NAMES } from './clipNames'
export type { ClipName } from './clipNames'
import { CLIP_DURATION, CLIP_LOOPS, type ClipName } from './clipNames'

const TAU = Math.PI * 2
const N = BONE_NAMES.length
const B = BONE_INDEX
/** Hand twist (rad) that turns the palm up when the forearm points forward. */
const PALM_UP = 1.3

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

/** 0 before `a`, 1 between `b` and `c`, 0 after `d`: the envelope of a one-shot gesture. */
function hold(t: number, a: number, b: number, c: number, d: number): number {
  return smooth(a, b, t) * (1 - smooth(c, d, t))
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

/**
 * Both legs at thigh angle `a` and knee bend `k` with the feet flat on the ground and the hips
 * re-planted over the ankles; `spread` opens the knees sideways. Sets py/pz (callers add to them).
 */
function legsPlanted(p: Pose, c: Ctx, a: number, k: number, spread: number): void {
  const [dy, dz] = plant(c, a, k)
  p.set(B.upperLegL, a, spread * 0.85, spread)
  p.set(B.upperLegR, a, -spread * 0.85, -spread)
  p.set(B.lowerLegL, k, 0, 0)
  p.set(B.lowerLegR, k, 0, 0)
  p.set(B.footL, -(a + k), 0, 0)
  p.set(B.footR, -(a + k), 0, 0)
  p.py = dy
  p.pz = dz
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

// ------------------------------------------------------------------ idles

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

/**
 * Restless standing (5 s): the weight rolls side to side once per loop, the free right foot taps
 * while the weight is on the left, the right hand scratches the back of the head around t = 1.5–2.3,
 * the left heel lifts to re-settle when the weight is on the right, and the head looks around.
 */
function fidgetPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const br = Math.sin((TAU * t) / 2.5)
  p.add(B.chest, 0.03 * br, 0, 0)
  p.add(B.shoulderL, 0, 0, 0.03 * br)
  p.add(B.shoulderR, 0, 0, -0.03 * br)
  // weight shift: hips over the left leg at t ≈ 1.25, over the right at t ≈ 3.75
  const sway = Math.sin((TAU * t) / 5)
  p.add(B.hips, 0, 0, 0.06 * sway)
  p.add(B.spine, 0.03, 0, -0.05 * sway)
  p.add(B.chest, 0, 0.05 * sway, -0.02 * sway)
  p.add(B.upperLegL, 0, 0, -0.08 * sway)
  p.add(B.upperLegR, 0, 0, -0.08 * sway)
  p.px = 0.07 * c.u * sway
  p.py = 0.006 * c.u * br
  // toe taps of the unloaded right foot
  const tapE = bump(t, 0.4, 1.7)
  const tap = Math.abs(Math.sin(TAU * 3 * (t - 0.4)))
  p.add(B.upperLegR, -0.06 * tapE, 0, 0)
  p.add(B.lowerLegR, 0.12 * tapE, 0, 0)
  p.add(B.footR, -0.06 * tapE - 0.45 * tap * tapE, 0, 0)
  // scratching the back of the head with the right hand
  const s = hold(t, 1.4, 1.75, 2.1, 2.45)
  const scrub = Math.sin(TAU * 4 * t) * s
  p.add(B.shoulderR, -0.1 * s, 0, -0.25 * s)
  p.set(B.upperArmR, 0.05 - 2.75 * s, 0.35 * s, -0.1 - 0.45 * s)
  p.set(B.forearmR, -0.16 - 2.3 * s + 0.12 * scrub, -0.05, -0.03)
  p.set(B.handR, -0.4 * s + 0.1 * scrub, 0, -0.05)
  p.add(B.head, 0.1 * s, -0.12 * s, 0.1 * s)
  p.add(B.neck, 0.05 * s, 0, 0.05 * s)
  p.add(B.chest, 0, 0, 0.04 * s)
  // the left heel lifts and re-settles while the weight is on the right
  const heel = bump(t, 3.3, 4.3)
  p.add(B.upperLegL, -0.14 * heel, 0, 0)
  p.add(B.lowerLegL, 0.32 * heel, 0, 0)
  p.add(B.footL, -0.08 * heel, 0, 0)
  // looking around: left, then far right with a tilt, then back left
  const look0 = bump(t, 0.2, 1.3)
  p.add(B.head, 0.02 * br, -0.35 * look0, -0.03 * look0)
  p.add(B.neck, 0, -0.1 * look0, 0)
  const look1 = bump(t, 2.6, 3.7)
  p.add(B.head, -0.03 * look1, 0.55 * look1, 0.05 * look1)
  p.add(B.neck, 0, 0.15 * look1, 0)
  const look2 = bump(t, 3.9, 4.8)
  p.add(B.head, 0.05 * look2, -0.4 * look2, -0.05 * look2)
  p.add(B.neck, 0, -0.1 * look2, 0)
}

/**
 * Still and closed (6 s): arms crossed over the chest, feet a little wider than rest, almost no sway,
 * a slow scan of the head.
 */
function guardPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const br = Math.sin((TAU * t) / 3)
  p.add(B.chest, 0.025 * br, 0, 0)
  // wider stance: legs opened sideways, hips a hair lower so the feet stay on the ground
  const spread = 0.12
  p.set(B.upperLegL, 0, 0, spread)
  p.set(B.upperLegR, 0, 0, -spread)
  p.py = -(c.thigh + c.shin) * (1 - Math.cos(spread))
  // crossed arms: upper arms forward and twisted inward so the bent forearms lie across the chest,
  // the right forearm over the left, each hand near the other elbow
  p.set(B.shoulderL, 0.08, 0, -0.04)
  p.set(B.shoulderR, 0.08, 0, 0.04)
  p.set(B.upperArmL, -0.55, -1.25, -0.12)
  p.set(B.forearmL, -2.05, 0, 0)
  p.set(B.handL, 0.2, 0, 0)
  p.set(B.upperArmR, -0.7, 1.25, 0.12)
  p.set(B.forearmR, -2.05, 0, 0)
  p.set(B.handR, 0.2, 0, 0)
  p.add(B.spine, 0.04, 0, 0)
  p.add(B.chest, -0.02, 0, 0)
  // the slow scan, chin slightly down
  const scan = Math.sin((TAU * t) / 6)
  p.add(B.head, 0.06, 0.4 * scan, 0)
  p.add(B.neck, 0.02, 0.12 * scan, 0)
  p.px = 0.006 * c.u * scan
}

// ------------------------------------------------------------------ gaits

interface Gait {
  /** Thigh swing amplitude (rad). */
  stride: number
  /** Knee bend of the swing leg (rad). */
  knee: number
  /** Upper arm swing amplitude (rad). */
  arm: number
  /** Extra forearm bend on the forward arm swing (rad). */
  elbow: number
  /** Forward lean of the spine (rad); chest, neck and head follow so the gaze stays level. */
  lean: number
  /** Vertical bob of the hips (head units). */
  bob: number
  /** Pelvis yaw / roll scale. */
  hip: number
}

const WALK: Gait = { stride: 0.55, knee: 1.05, arm: 0.5, elbow: 0.4, lean: 0.06, bob: 0.12, hip: 1 }
const BRISK: Gait = { stride: 0.7, knee: 1.2, arm: 0.75, elbow: 0.55, lean: 0.22, bob: 0.16, hip: 1.3 }

/**
 * One gait cycle at normalised phase `s` (0..1). Every gait uses this leg-phase function with its
 * own amplitudes, so blends between gaits at matching time scales do not foot-slide.
 */
function gaitPose(s: number, p: Pose, c: Ctx, g: Gait): void {
  rest(p)
  const phi = TAU * s
  const leg = (ph: number, ul: number, ll: number, ft: number, sgn: number) => {
    const a = -g.stride * Math.sin(ph)
    const c0 = Math.max(0, Math.cos(ph))
    const k = 0.12 + g.knee * Math.pow(c0, 1.6)
    const push = Math.max(0, -Math.sin(ph - 0.5))
    p.set(ul, a, 0, sgn * 0.03)
    p.set(ll, k, 0, 0)
    p.set(ft, -(a + k) * 0.55 + 0.35 * push * push, 0, 0)
  }
  leg(phi, B.upperLegL, B.lowerLegL, B.footL, 1)
  leg(phi + Math.PI, B.upperLegR, B.lowerLegR, B.footR, -1)
  const arm = (ph: number, ua: number, fa: number, sgn: number) => {
    const sn = Math.sin(ph)
    p.set(ua, g.arm * sn, 0, sgn * 0.12)
    p.set(fa, -0.25 - g.elbow * Math.max(0, -sn), sgn * 0.08, sgn * 0.04)
  }
  arm(phi, B.upperArmL, B.forearmL, 1)
  arm(phi + Math.PI, B.upperArmR, B.forearmR, -1)
  const sn = Math.sin(phi)
  const dl = g.lean - WALK.lean
  p.set(B.hips, 0.02, -0.12 * g.hip * sn, 0.06 * g.hip * sn)
  p.set(B.spine, g.lean, 0.08 * sn, -0.03 * sn)
  p.set(B.chest, 0.03 + 0.5 * dl, 0.07 * sn, -0.02 * sn)
  p.set(B.neck, -0.03 - 0.7 * dl, -0.03 * sn, 0)
  p.set(B.head, -0.05 - 0.6 * dl, 0, 0)
  p.py = g.bob * c.u * Math.cos(2 * phi)
}

function walkPose(t: number, p: Pose, c: Ctx): void {
  gaitPose(t / CLIP_DURATION.walk, p, c, WALK)
}

function briskPose(t: number, p: Pose, c: Ctx): void {
  gaitPose(t / CLIP_DURATION.walk_brisk, p, c, BRISK)
}

// ------------------------------------------------------------------ states

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

// ------------------------------------------------------------------ gestures

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

/**
 * Writing (2 s loop): a notepad in the left hand in front of the sternum, the right hand scribbling
 * over it in 3 Hz strokes that drift along the page, head down on the pad; the strokes pause and
 * the head nods at the end of the loop.
 */
function writePose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const stroke = 1 - hold(t, 1.45, 1.6, 1.85, 2.0)
  const drift = hold(t, 0.1, 1.45, 1.6, 1.95)
  const osc = Math.sin(TAU * 3 * t) * stroke
  const osc2 = Math.sin(TAU * 3 * t + 1.2) * stroke
  // the pad: left arm forward, twisted inward, forearm bent across so the hand sits before the sternum
  p.set(B.shoulderL, 0.05, 0, 0)
  p.set(B.upperArmL, -0.9, -0.9, -0.15)
  p.set(B.forearmL, -1.95, 0, 0)
  p.set(B.handL, 0.35, 0, 0)
  // the pen hand: slightly higher, drifting from the left to the right of the page
  p.set(B.shoulderR, 0.05, 0, 0)
  p.set(B.upperArmR, -0.75 + 0.05 * osc2, 0.85 - 0.25 * drift, 0.15)
  p.set(B.forearmR, -2.0 + 0.08 * osc, 0, 0)
  p.set(B.handR, -0.25 + 0.18 * osc2, 0, 0.22 * osc)
  // bent over the pad
  p.add(B.spine, 0.08, 0, 0)
  p.add(B.chest, 0.06, 0.05, 0)
  p.add(B.neck, 0.15, 0, 0)
  const nod = bump(t, 1.55, 1.95)
  p.add(B.head, 0.35 + 0.12 * nod, 0.12, 0.04)
}

/**
 * Reading (3 s loop): both hands hold a tablet at chest height, head down, a slow sway of the whole
 * body; the right hand lifts and sweeps a page over around t = 2.
 */
function readPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const sway = Math.sin((TAU * t) / 3)
  const br = Math.sin((TAU * t) / 1.5)
  p.add(B.hips, 0, 0, 0.02 * sway)
  p.add(B.spine, 0.06, 0, -0.015 * sway)
  p.add(B.chest, 0.05 + 0.02 * br, 0, 0)
  p.add(B.upperLegL, 0, 0, -0.025 * sway)
  p.add(B.upperLegR, 0, 0, -0.025 * sway)
  p.px = 0.025 * c.u * sway
  p.py = 0.005 * c.u * br
  // the tablet: both arms forward, twisted inward, forearms bent so the hands meet before the chest
  p.set(B.upperArmL, -0.8, -0.75, -0.1)
  p.set(B.forearmL, -1.9, 0, 0)
  p.set(B.handL, 0.3, 0, 0)
  const turn = bump(t, 1.55, 2.45)
  const sweep = Math.sin(TAU * smooth(1.55, 2.45, t)) * turn
  p.set(B.upperArmR, -0.8 - 0.1 * turn, 0.75 - 0.45 * sweep, 0.1 + 0.1 * turn)
  p.set(B.forearmR, -1.9 - 0.25 * turn, 0, 0)
  p.set(B.handR, 0.3 - 0.4 * turn, 0, 0.35 * sweep)
  // head down on the page, eyes following the turn
  p.add(B.neck, 0.15, 0, 0)
  p.add(B.head, 0.4, 0.08 * sweep, 0.02 * sway)
}

/**
 * Giving (1.4 s one-shot): a small step of the hips forward with the feet planted, both arms extend
 * forward at waist height with open, palm-up hands, hold from t ≈ 0.55 to 0.9, then back to rest.
 */
function givePose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const e = hold(t, 0.05, 0.55, 0.9, 1.35)
  // the step: legs lean back from the feet so the hips move 0.12 u forward
  const a = 0.035 * e
  legsPlanted(p, c, a, 0, 0.02)
  p.add(B.upperArmL, -0.95 * e, -0.2 * e, -0.22 * e)
  p.add(B.upperArmR, -0.95 * e, 0.2 * e, 0.22 * e)
  p.add(B.forearmL, 0.1 * e, -0.05 * e, -0.03 * e)
  p.add(B.forearmR, 0.1 * e, 0.05 * e, 0.03 * e)
  p.set(B.handL, 0.25 * e, PALM_UP * e, 0.05 * (1 - e))
  p.set(B.handR, 0.25 * e, -PALM_UP * e, -0.05 * (1 - e))
  p.add(B.spine, 0.12 * e, 0, 0)
  p.add(B.chest, 0.05 * e, 0, 0)
  p.add(B.neck, -0.04 * e, 0, 0)
  p.add(B.head, 0.04 * e, 0, 0)
}

/**
 * Receiving (1.2 s one-shot): the hands cup forward low with the palms up, then draw to the chest
 * with a small nod, then back to rest.
 */
function receivePose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const cup = hold(t, 0, 0.35, 0.5, 0.8)
  const draw = hold(t, 0.5, 0.8, 0.9, 1.2)
  const e = cup + draw
  p.add(B.upperArmL, -0.65 * cup - 0.45 * draw, -0.55 * e, -0.3 * e)
  p.add(B.upperArmR, -0.65 * cup - 0.45 * draw, 0.55 * e, 0.3 * e)
  p.add(B.forearmL, -0.95 * cup - 2.0 * draw, -0.05 * e, -0.03 * e)
  p.add(B.forearmR, -0.95 * cup - 2.0 * draw, 0.05 * e, 0.03 * e)
  p.set(B.handL, 0.25 * cup + 0.1 * draw, PALM_UP * e, 0.05 * (1 - e))
  p.set(B.handR, 0.25 * cup + 0.1 * draw, -PALM_UP * e, -0.05 * (1 - e))
  p.add(B.spine, 0.08 * cup + 0.02 * draw, 0, 0)
  p.add(B.chest, 0.03 * cup - 0.04 * draw, 0, 0)
  const nod = bump(t, 0.6, 1.1)
  p.add(B.neck, 0.05 * cup + 0.06 * nod, 0, 0)
  p.add(B.head, 0.12 * cup + 0.18 * nod, 0, 0)
}

/**
 * Tinkering (2 s loop): a crouch, more upright than forage, the hands working at knee height: the right
 * hand turns something while the left steadies it, the head on the hands.
 */
function tinkerPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const w = Math.sin(TAU * 1.5 * t)
  const w2 = Math.sin(TAU * 1.5 * t + 1.0)
  legsPlanted(p, c, -1.0, 1.2, 0.14)
  p.set(B.hips, 0.08, 0, 0)
  p.set(B.spine, 0.35, 0.02 * w, 0)
  p.set(B.chest, 0.15, 0, 0)
  p.set(B.neck, 0.12, 0, 0)
  p.set(B.head, 0.45, -0.06 + 0.04 * w, 0)
  // the turning hand
  p.set(B.upperArmR, -0.5 + 0.04 * w2, 0.35, 0.05)
  p.set(B.forearmR, -1.05 + 0.12 * w2, 0, 0)
  p.set(B.handR, -0.15 + 0.1 * w2, -0.7 * w, 0)
  // the steadying hand
  p.set(B.upperArmL, -0.45 - 0.03 * w, -0.35, -0.05)
  p.set(B.forearmL, -1.0 - 0.06 * w, 0, 0)
  p.set(B.handL, 0.15 + 0.05 * w, 0.3, 0)
  p.py += 0.01 * c.u * w2
}

/**
 * Summoning (1.6 s one-shot): both arms rise overhead, hold with a sideways sweep at the top while the
 * chest opens and the head looks up, then down to rest.
 */
function summonPose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const e = hold(t, 0.05, 0.5, 1.1, 1.55)
  const top = bump(t, 0.5, 1.1)
  const sweep = Math.sin(TAU * smooth(0.5, 1.1, t)) * top
  p.add(B.shoulderL, -0.05 * e, 0, 0.12 * e)
  p.add(B.shoulderR, -0.05 * e, 0, -0.12 * e)
  p.add(B.upperArmL, -2.65 * e, 0, 0.25 * e + 0.3 * sweep)
  p.add(B.upperArmR, -2.65 * e, 0, -0.25 * e + 0.3 * sweep)
  p.add(B.forearmL, -0.25 * e, 0, 0.1 * e)
  p.add(B.forearmR, -0.25 * e, 0, -0.1 * e)
  p.add(B.handL, -0.3 * e, 0, 0.15 * e)
  p.add(B.handR, -0.3 * e, 0, -0.15 * e)
  p.add(B.spine, -0.15 * e, 0, 0.03 * sweep)
  p.add(B.chest, -0.1 * e, 0, 0.03 * sweep)
  p.add(B.neck, -0.15 * e, 0, 0)
  p.add(B.head, -0.4 * e, 0, -0.04 * sweep)
  p.py = 0.03 * _c.u * e
}

/**
 * Thinking (3 s loop): weight on the left leg, the right hand on the chin with the left arm across the
 * belly supporting the elbow, the head tilted and slowly turning, a look up around t = 1.5.
 */
function thinkPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const br = Math.sin((TAU * t) / 3)
  const turn = Math.sin((TAU * t) / 3)
  const up = bump(t, 1.15, 2.1)
  // weight on the left leg: hips shifted over it and tilted, the right knee soft
  p.set(B.hips, 0.02, 0.05, 0.06)
  p.set(B.upperLegL, 0, 0, -0.055)
  p.set(B.upperLegR, -0.08, 0, -0.1)
  p.set(B.lowerLegR, 0.2, 0, 0)
  p.set(B.footR, -0.12, 0, 0)
  p.px = 0.06 * c.u
  p.py = 0.005 * c.u * br
  // right hand on the chin
  p.set(B.shoulderR, 0.05, 0, 0.03)
  p.set(B.upperArmR, -0.5, 0.95, 0.2)
  p.set(B.forearmR, -2.2, 0, 0)
  p.set(B.handR, -0.5, 0, 0)
  // left arm across the belly under the right elbow
  p.set(B.upperArmL, -0.35, -1.05, -0.15)
  p.set(B.forearmL, -1.7, 0, 0)
  p.set(B.handL, 0.1, 0, 0)
  p.set(B.spine, 0.06, -0.04, -0.03)
  p.set(B.chest, 0.04 + 0.025 * br, -0.06, 0)
  p.set(B.neck, 0.04 - 0.1 * up, 0.08 * turn, 0.04)
  p.set(B.head, 0.1 - 0.35 * up, 0.25 * turn - 0.1 * up, 0.15 - 0.05 * up)
}

/**
 * Shaking the head (1 s one-shot): three half-cycles of yaw, the shoulders lift briefly and the forearms
 * turn palm-up for a beat, then rest.
 */
function shakePose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const env = hold(t, 0, 0.12, 0.8, 1.0)
  const yaw = 0.4 * Math.sin((3 * Math.PI * Math.min(t, 0.9)) / 0.9) * env
  p.add(B.head, -0.04 * env, yaw, -0.15 * yaw)
  p.add(B.neck, 0, 0.3 * yaw, 0)
  const sh = bump(t, 0.05, 0.65)
  p.add(B.shoulderL, 0, 0, 0.22 * sh)
  p.add(B.shoulderR, 0, 0, -0.22 * sh)
  p.add(B.spine, -0.03 * sh, 0, 0)
  const pu = bump(t, 0.1, 0.85)
  p.add(B.upperArmL, 0, -0.2 * pu, 0.18 * pu)
  p.add(B.upperArmR, 0, 0.2 * pu, -0.18 * pu)
  p.add(B.forearmL, -0.6 * pu, 0.3 * pu, 0)
  p.add(B.forearmR, -0.6 * pu, -0.3 * pu, 0)
  p.set(B.handL, 0.1 * pu, PALM_UP * pu, 0.05 * (1 - pu))
  p.set(B.handR, 0.1 * pu, -PALM_UP * pu, -0.05 * (1 - pu))
}

/**
 * Cheering (1.2 s one-shot): both arms thrust up with a small hop (knees bend before and after; the feet
 * leave the ground only while the hips are up), head up, then rest.
 */
function cheerPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const bend = 0.8 * bump(t, 0, 0.44) + 0.7 * bump(t, 0.62, 1.15)
  legsPlanted(p, c, -0.3 * bend, 0.6 * bend, 0.04)
  const hop = bump(t, 0.3, 0.76)
  p.add(B.upperLegL, -0.15 * hop, 0, 0)
  p.add(B.upperLegR, -0.15 * hop, 0, 0)
  p.add(B.lowerLegL, 0.35 * hop, 0, 0)
  p.add(B.lowerLegR, 0.35 * hop, 0, 0)
  p.add(B.footL, 0.1 * hop, 0, 0)
  p.add(B.footR, 0.1 * hop, 0, 0)
  p.py += 0.12 * c.u * hop
  const e = hold(t, 0.12, 0.42, 0.8, 1.15)
  const pump = 0.15 * Math.sin(TAU * 2 * (t - 0.42)) * bump(t, 0.42, 0.9)
  p.add(B.shoulderL, -0.05 * e, 0, 0.15 * e)
  p.add(B.shoulderR, -0.05 * e, 0, -0.15 * e)
  p.add(B.upperArmL, -2.75 * e + pump, 0, 0.3 * e)
  p.add(B.upperArmR, -2.75 * e + pump, 0, -0.3 * e)
  p.add(B.forearmL, -0.45 * e, 0, 0.05 * e)
  p.add(B.forearmR, -0.45 * e, 0, -0.05 * e)
  p.add(B.handL, -0.3 * e, 0, 0)
  p.add(B.handR, -0.3 * e, 0, 0)
  p.add(B.spine, -0.1 * e + 0.08 * bend, 0, 0)
  p.add(B.chest, -0.05 * e, 0, 0)
  p.add(B.neck, -0.1 * e, 0, 0)
  p.add(B.head, -0.3 * e, 0, 0)
}

/**
 * Slumping (1.4 s one-shot): the shoulders drop, the spine curls forward, the head hangs and the hands
 * sink toward the knees as they bend a little; a hold, then a slow straightening back to rest.
 */
function slumpPose(t: number, p: Pose, c: Ctx): void {
  rest(p)
  const e = hold(t, 0, 0.45, 0.85, 1.4)
  legsPlanted(p, c, -0.2 * e, 0.4 * e, 0.02)
  p.add(B.shoulderL, 0.1 * e, 0, -0.2 * e)
  p.add(B.shoulderR, 0.1 * e, 0, 0.2 * e)
  p.add(B.spine, 0.4 * e, 0, 0)
  p.add(B.chest, 0.2 * e, 0, 0)
  p.add(B.neck, 0.2 * e, 0, 0)
  p.add(B.head, 0.4 * e, 0, 0)
  // the torso tilts forward by about 0.75 rad and the arms hang from it: rotate them back by that much plus a
  // little so the hands fall in front of the knees instead of swinging behind the body
  p.add(B.upperArmL, -0.9 * e, 0, -0.05 * e)
  p.add(B.upperArmR, -0.9 * e, 0, 0.05 * e)
  p.add(B.forearmL, -0.15 * e, 0, 0)
  p.add(B.forearmR, -0.15 * e, 0, 0)
  p.add(B.handL, 0.1 * e, 0, 0)
  p.add(B.handR, 0.1 * e, 0, 0)
}

/**
 * Waving (1.2 s one-shot): the right arm rises sideways-up with the forearm bent, the hand waves at
 * 3 Hz, the head tilts toward the raised arm, then rest.
 */
function wavePose(t: number, p: Pose, _c: Ctx): void {
  rest(p)
  const e = hold(t, 0.05, 0.35, 0.85, 1.2)
  const wag = Math.sin(TAU * 3 * t) * e
  p.add(B.shoulderR, -0.05 * e, 0, -0.2 * e)
  p.add(B.upperArmR, -0.35 * e, 0, -1.8 * e)
  p.add(B.forearmR, -0.5 * e, 0, -0.9 * e)
  p.set(B.handR, -0.15 * e, 0, -0.05 * (1 - e) + 0.5 * wag)
  p.add(B.chest, 0, 0, -0.04 * e)
  p.add(B.neck, 0, -0.05 * e, -0.05 * e)
  p.add(B.head, -0.03 * e, -0.12 * e, -0.15 * e)
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
 * Build every clip in CLIP_NAMES for a skeleton. `bones` gives the rest hips position (position tracks
 * are absolute); `preset` gives the head unit and limb lengths. Loops are sampled at 8–16 fps, one-shots
 * at 16–24 fps (the 3 Hz hand oscillations get at least five keys per cycle).
 */
export function buildClips(bones: Record<BoneName, Bone>, preset: Preset): Record<ClipName, AnimationClip> {
  const u = preset.height / 7.5
  const h = bones.hips.position
  const c: Ctx = { u, hipsRest: [h.x, h.y, h.z], thigh: 1.75 * u, shin: 1.7 * u }
  const loop = (name: ClipName, fps: number, fn: (t: number, p: Pose, c: Ctx) => void): AnimationClip =>
    sampled(name, CLIP_DURATION[name], fps, fn, c, CLIP_LOOPS[name])
  return {
    idle: loop('idle', 8, idlePose),
    idle_fidget: loop('idle_fidget', 16, fidgetPose),
    idle_guard: loop('idle_guard', 8, guardPose),
    walk: loop('walk', 24, walkPose),
    walk_brisk: loop('walk_brisk', 24, briskPose),
    sleep: loop('sleep', 8, sleepPose),
    degenerate: stepped('degenerate', CLIP_DURATION.degenerate, 20, degeneratePose, c),
    dead: keyed('dead', deadKeys(c), c),
    talk: loop('talk', 20, talkPose),
    forage: loop('forage', 16, foragePose),
    write: loop('write', 16, writePose),
    read: loop('read', 12, readPose),
    give: loop('give', 20, givePose),
    receive: loop('receive', 20, receivePose),
    tinker: loop('tinker', 16, tinkerPose),
    summon: loop('summon', 20, summonPose),
    think: loop('think', 12, thinkPose),
    shake: loop('shake', 24, shakePose),
    cheer: loop('cheer', 24, cheerPose),
    slump: loop('slump', 16, slumpPose),
    wave: loop('wave', 24, wavePose),
  }
}
