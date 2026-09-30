/**
 * Procedural stylised humanoid: a 19-bone hierarchy (hips → spine → chest →
 * neck → head, clavicles → arms → hands, legs → feet) with skinned geometry
 * built from revolved profiles, spheres, boxes and tori. Every part carries
 * per-vertex skin indices/weights so limbs bend at the joints (weights blend
 * within a small radius of each joint), and vertex colours for skin, hair,
 * clothing and shoes, so the whole body is one draw call. Tier-tinted trim
 * (headband, belt, wrist band) is a second skinned mesh with its own material.
 *
 * Rest pose: facing +Z, feet on y = 0, arms hanging, top of the skull at
 * `preset.height`. Proportions follow a 7.5-head figure guide: head ≈ 1/7.5 of
 * the height, shoulders ≈ 2 heads wide, fingertips at mid-thigh.
 */
import {
  Bone,
  BoxGeometry,
  BufferGeometry,
  Color,
  CylinderGeometry,
  Float32BufferAttribute,
  Group,
  Matrix4,
  MeshStandardMaterial,
  Skeleton,
  SkinnedMesh,
  SphereGeometry,
  TorusGeometry,
  Uint16BufferAttribute,
} from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import type { Preset } from './presets'

export const BONE_NAMES = [
  'hips',
  'spine',
  'chest',
  'neck',
  'head',
  'shoulderL',
  'shoulderR',
  'upperArmL',
  'upperArmR',
  'forearmL',
  'forearmR',
  'handL',
  'handR',
  'upperLegL',
  'upperLegR',
  'lowerLegL',
  'lowerLegR',
  'footL',
  'footR',
] as const

export type BoneName = (typeof BONE_NAMES)[number]

export const BONE_INDEX: Readonly<Record<BoneName, number>> = Object.fromEntries(BONE_NAMES.map((n, i) => [n, i])) as Record<BoneName, number>

export interface Humanoid {
  root: Group
  skinned: SkinnedMesh[]
  bones: Record<BoneName, Bone>
  /** Ordered like BONE_NAMES (skin indices refer to this order). */
  boneList: Bone[]
  skeleton: Skeleton
  /** Standing height (top of the skull) in world units. */
  height: number
  body: SkinnedMesh
  trim: SkinnedMesh
  materials: { body: MeshStandardMaterial; trim: MeshStandardMaterial }
  /** Rest-pose landmarks used by the clip authoring (units). */
  dims: HumanDims
  dispose(): void
}

/** Rest-pose landmarks, all in world units, derived from the head unit `u`. */
export interface HumanDims {
  u: number
  height: number
  hipsY: number
  hipJointY: number
  thighLen: number
  shinLen: number
  ankleY: number
  shoulderY: number
  shoulderX: number
  upperArmLen: number
  forearmLen: number
}

export function dimsFor(preset: Preset): HumanDims {
  const u = preset.height / 7.5
  return {
    u,
    height: preset.height,
    hipsY: 3.9 * u,
    hipJointY: 3.75 * u,
    thighLen: 1.75 * u,
    shinLen: 1.7 * u,
    ankleY: 0.3 * u,
    shoulderY: 6.05 * u,
    shoulderX: 1.0 * u * preset.shoulder,
    upperArmLen: 1.4 * u,
    forearmLen: 1.15 * u,
  }
}

// ------------------------------------------------------------------ helpers

const TAU = Math.PI * 2

type Profile = ReadonlyArray<readonly [number, number]>

/**
 * Revolve a (radius, y) profile around Y. `sx`/`sz` squash the cross-section
 * into an ellipse; `a0`/`a1` (radians) limit the sweep for partial shells.
 */
function revolve(profile: Profile, radial: number, sx = 1, sz = 1, a0 = 0, a1 = TAU): BufferGeometry {
  const rings = profile.length
  const pos: number[] = []
  const uv: number[] = []
  const idx: number[] = []
  const full = Math.abs(a1 - a0 - TAU) < 1e-6
  const cols = radial + 1
  for (let i = 0; i < rings; i++) {
    const [r, y] = profile[i]!
    for (let j = 0; j < cols; j++) {
      const a = a0 + ((a1 - a0) * j) / radial
      pos.push(Math.cos(a) * r * sx, y, Math.sin(a) * r * sz)
      uv.push(j / radial, i / Math.max(1, rings - 1))
    }
  }
  for (let i = 0; i < rings - 1; i++) {
    for (let j = 0; j < radial; j++) {
      const a = i * cols + j
      const b = a + cols
      idx.push(a, b, a + 1, b, b + 1, a + 1)
    }
  }
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(pos, 3))
  g.setAttribute('uv', new Float32BufferAttribute(uv, 2))
  g.setIndex(idx)
  g.computeVertexNormals()
  if (!full) {
    // open shell: nothing else to do, normals already point outward
  }
  return g
}

/** Capsule-like limb profile from yBottom to yTop with a linear taper. */
function limbProfile(rTop: number, rBottom: number, yTop: number, yBottom: number, capSegs = 2, midRings = 3): Profile {
  const out: Array<[number, number]> = []
  out.push([0, yBottom - rBottom])
  for (let i = 1; i <= capSegs; i++) {
    const a = (i / capSegs) * (Math.PI / 2)
    out.push([rBottom * Math.sin(a), yBottom - rBottom * Math.cos(a)])
  }
  for (let i = 1; i < midRings; i++) {
    const t = i / midRings
    out.push([rBottom + (rTop - rBottom) * t, yBottom + (yTop - yBottom) * t])
  }
  for (let i = 0; i < capSegs; i++) {
    const a = (i / capSegs) * (Math.PI / 2)
    out.push([rTop * Math.cos(a), yTop + rTop * Math.sin(a)])
  }
  out.push([0, yTop + rTop])
  return out
}

type SkinFn = (x: number, y: number, z: number, out: Float32Array) => void
type ColorFn = Color | ((x: number, y: number, z: number, out: Color) => void)

function rigid(bone: number): SkinFn {
  return (_x, _y, _z, out) => {
    out[0] = bone
    out[1] = 1
    out[2] = bone
    out[3] = 0
  }
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v
}

/**
 * Chain skinning along Y: each node is a bone rooted at a joint height, the
 * geometry between node i and i+1 belongs to bone i and blends into bone i+1
 * within ±r of that joint (a smooth partition of unity, at most two bones per
 * vertex). Works upward (torso) or downward (limbs) from the first node.
 */
function chain(nodes: ReadonlyArray<readonly [number, number]>, r: number): SkinFn {
  const dir = nodes.length > 1 && nodes[1]![1] < nodes[0]![1] ? -1 : 1
  const y0 = nodes[0]![1]
  const s = nodes.map(([, y]) => (y - y0) * dir)
  const ids = nodes.map(([b]) => b)
  const n = ids.length
  return (_x, y, _z, out) => {
    const sv = (y - y0) * dir
    let bestI = ids[0]!
    let bestW = 0
    let secondI = ids[0]!
    let secondW = 0
    let prevUp = 1
    for (let j = 0; j < n; j++) {
      const nextUp = j + 1 < n ? clamp01(0.5 + (sv - s[j + 1]!) / (2 * r)) : 0
      const w = prevUp - nextUp
      if (w > bestW) {
        secondI = bestI
        secondW = bestW
        bestI = ids[j]!
        bestW = w
      } else if (w > secondW) {
        secondI = ids[j]!
        secondW = w
      }
      prevUp = nextUp
    }
    const sum = bestW + secondW || 1
    out[0] = bestI
    out[1] = bestW / sum
    out[2] = secondI
    out[3] = secondW / sum
  }
}

class PartBuilder {
  private parts: BufferGeometry[] = []
  private tmp = new Float32Array(4)
  private col = new Color()

  add(g: BufferGeometry, color: ColorFn, skin: SkinFn): void {
    const pos = g.getAttribute('position')
    const n = pos.count
    if (!g.getAttribute('uv')) g.setAttribute('uv', new Float32BufferAttribute(new Float32Array(n * 2), 2))
    if (!g.getAttribute('normal')) g.computeVertexNormals()
    const colors = new Float32Array(n * 3)
    const si = new Uint16Array(n * 4)
    const sw = new Float32Array(n * 4)
    const tmp = this.tmp
    const col = this.col
    for (let i = 0; i < n; i++) {
      const x = pos.getX(i)
      const y = pos.getY(i)
      const z = pos.getZ(i)
      if (color instanceof Color) col.copy(color)
      else color(x, y, z, col)
      colors[i * 3] = col.r
      colors[i * 3 + 1] = col.g
      colors[i * 3 + 2] = col.b
      skin(x, y, z, tmp)
      si[i * 4] = tmp[0]!
      si[i * 4 + 1] = tmp[2]!
      sw[i * 4] = tmp[1]!
      sw[i * 4 + 1] = tmp[3]!
    }
    g.setAttribute('color', new Float32BufferAttribute(colors, 3))
    g.setAttribute('skinIndex', new Uint16BufferAttribute(si, 4))
    g.setAttribute('skinWeight', new Float32BufferAttribute(sw, 4))
    this.parts.push(g)
  }

  get count(): number {
    return this.parts.length
  }

  build(): BufferGeometry {
    if (this.parts.length === 0) {
      const g = new BufferGeometry()
      g.setAttribute('position', new Float32BufferAttribute(new Float32Array(0), 3))
      g.setAttribute('normal', new Float32BufferAttribute(new Float32Array(0), 3))
      g.setAttribute('uv', new Float32BufferAttribute(new Float32Array(0), 2))
      g.setAttribute('color', new Float32BufferAttribute(new Float32Array(0), 3))
      g.setAttribute('skinIndex', new Uint16BufferAttribute(new Uint16Array(0), 4))
      g.setAttribute('skinWeight', new Float32BufferAttribute(new Float32Array(0), 4))
      g.setIndex([])
      return g
    }
    const merged = mergeGeometries(this.parts, false)
    if (!merged) throw new Error('humanoid: part attribute mismatch')
    for (const p of this.parts) p.dispose()
    this.parts = []
    merged.computeBoundingSphere()
    merged.computeBoundingBox()
    return merged
  }
}

function sphere(r: number, sx: number, sy: number, sz: number, x: number, y: number, z: number, ws = 8, hs = 6): BufferGeometry {
  const g = new SphereGeometry(r, ws, hs)
  g.scale(sx, sy, sz)
  g.translate(x, y, z)
  return g
}

function box(w: number, h: number, d: number, x: number, y: number, z: number): BufferGeometry {
  const g = new BoxGeometry(w, h, d)
  g.translate(x, y, z)
  return g
}

/** Horizontal ring (lies in XZ) centred at (x, y, z). */
function ring(R: number, tube: number, x: number, y: number, z: number, sx = 1, sz = 1, seg = 14): BufferGeometry {
  const g = new TorusGeometry(R, tube, 4, seg)
  g.rotateX(Math.PI / 2)
  g.scale(sx, 1, sz)
  g.translate(x, y, z)
  return g
}

function shade(c: Color, k: number): Color {
  return c.clone().multiplyScalar(k)
}

/** Deterministic unit hash for placing curls etc. */
function hash(n: number): number {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453
  return x - Math.floor(x)
}

// ------------------------------------------------------------------ build

export function buildHumanoid(preset: Preset): Humanoid {
  const D = dimsFor(preset)
  const u = D.u
  const B = BONE_INDEX
  const limb = preset.limb

  // landmarks
  const yTop = 7.5 * u
  const yChin = 6.5 * u
  const yHeadC = 7.0 * u
  const yNeckBase = 6.15 * u
  const yChest = 5.1 * u
  const ySpine = 4.4 * u
  const yHips = D.hipsY
  const yCrotch = 3.72 * u
  const yShoulder = D.shoulderY
  const xShoulder = D.shoulderX
  const xClav = 0.3 * u * preset.shoulder
  const xHip = 0.42 * u * preset.hip
  const yHipJ = D.hipJointY
  const yKnee = yHipJ - D.thighLen
  const yAnkle = D.ankleY
  const yElbow = yShoulder - D.upperArmLen
  const yWrist = yElbow - D.forearmLen
  const sz = 0.62 * preset.bulk

  // ---- bones
  const bones = {} as Record<BoneName, Bone>
  const mk = (name: BoneName, parent: Bone | null, x: number, y: number, z: number) => {
    const b = new Bone()
    b.name = name
    b.position.set(x, y, z)
    if (parent) parent.add(b)
    bones[name] = b
    return b
  }
  const hips = mk('hips', null, 0, yHips, 0)
  const spine = mk('spine', hips, 0, ySpine - yHips, 0)
  const chest = mk('chest', spine, 0, yChest - ySpine, 0)
  const neck = mk('neck', chest, 0, yNeckBase - yChest, 0)
  mk('head', neck, 0, yChin - yNeckBase, 0)
  for (const side of ['L', 'R'] as const) {
    const sgn = side === 'L' ? 1 : -1
    const sh = mk(`shoulder${side}`, chest, sgn * xClav, yShoulder - yChest, 0)
    const ua = mk(`upperArm${side}`, sh, sgn * (xShoulder - xClav), 0, 0)
    const fa = mk(`forearm${side}`, ua, 0, -D.upperArmLen, 0)
    mk(`hand${side}`, fa, 0, -D.forearmLen, 0)
    const ul = mk(`upperLeg${side}`, hips, sgn * xHip, yHipJ - yHips, 0)
    const ll = mk(`lowerLeg${side}`, ul, 0, -D.thighLen, 0)
    mk(`foot${side}`, ll, 0, -D.shinLen, 0)
  }
  const boneList = BONE_NAMES.map((n) => bones[n])

  // ---- colours
  const skin = new Color(preset.skin)
  const hairC = new Color(preset.hairColor)
  const topC = new Color(preset.topColor)
  const shirtC = new Color(preset.shirtColor)
  const botC = new Color(preset.bottomColor)
  const shoeC = new Color(preset.shoeColor)
  const accentC = new Color(preset.accentColor)
  const dark = new Color('#1c1a1c')
  const skinDark = shade(skin, 0.72)

  const body = new PartBuilder()
  const trim = new PartBuilder()

  // ---- torso (hips → chest) as one revolved shell
  const hipW = preset.hip
  const shW = preset.shoulder
  const waistW = 0.5 * hipW + 0.5 * shW
  const jacket = preset.top === 'jacket'
  const torsoScale = jacket ? 1.06 : 1
  const torsoProfile: Profile = [
    [0, yCrotch - 0.12 * u],
    [0.52 * u * hipW, yCrotch],
    [0.74 * u * hipW, 3.95 * u],
    [0.7 * u * hipW, 4.3 * u],
    [0.62 * u * waistW, 4.7 * u],
    [0.72 * u * shW, 5.3 * u],
    [0.84 * u * shW, 5.75 * u],
    [0.98 * u * shW, 6.0 * u],
    [0.86 * u * shW, 6.2 * u],
    [0.36 * u, 6.36 * u],
    [0, 6.42 * u],
  ]
  const torsoSkin = chain(
    [
      [B.hips, yHips],
      [B.spine, ySpine],
      [B.chest, yChest],
    ],
    0.3 * u,
  )
  const hemY = jacket ? 4.25 * u : 4.45 * u
  const torsoColor = (x: number, y: number, z: number, out: Color) => {
    if (y < hemY) {
      out.copy(preset.top === 'dress' || preset.top === 'tunic' ? topC : botC)
      return
    }
    if (preset.top === 'vest') {
      // V of shirt on the front, shirt sleeves; the vest covers the rest of the torso
      const v = 0.12 * u + 0.28 * (y - 4.45 * u)
      if (z > 0 && Math.abs(x) < v) out.copy(shirtC)
      else if (y > 6.12 * u) out.copy(shirtC)
      else out.copy(topC)
      return
    }
    if (jacket) {
      const strip = 0.16 * u
      if (z > 0.1 * u && Math.abs(x) < strip && y < 6.0 * u) out.copy(shirtC)
      else out.copy(topC)
      return
    }
    out.copy(topC)
  }
  body.add(revolve(torsoProfile, 16, torsoScale, sz * torsoScale), torsoColor, torsoSkin)

  // skirt / long tunic hem, rigid to the hips
  if (preset.top === 'dress' || preset.top === 'tunic') {
    const hem = preset.top === 'dress' ? 2.75 * u : 3.25 * u
    const skirt: Profile = [
      [0.71 * u * hipW, 4.5 * u],
      [0.78 * u * hipW, 3.95 * u],
      [0.86 * u * hipW, 3.45 * u],
      [0.98 * u * hipW, hem],
    ]
    body.add(revolve(skirt, 16, 1, 0.72 * preset.bulk), topC, rigid(B.hips))
  }

  // jacket collar
  if (jacket) {
    body.add(ring(0.3 * u, 0.075 * u, 0, yNeckBase + 0.08 * u, -0.02 * u, 1.05, 0.85), topC, rigid(B.chest))
  }

  // ---- neck
  body.add(
    revolve(
      [
        [0.19 * u, 5.95 * u],
        [0.16 * u, 6.3 * u],
        [0.17 * u, 6.65 * u],
      ],
      10,
      1,
      0.95,
    ),
    skin,
    chain(
      [
        [B.neck, yNeckBase],
        [B.head, yChin],
      ],
      0.12 * u,
    ),
  )

  // ---- head (egg-shaped skull), face
  const headProfile: Profile = [
    [0, yHeadC - 0.5 * u],
    [0.2 * u, yHeadC - 0.44 * u],
    [0.34 * u, yHeadC - 0.3 * u],
    [0.43 * u, yHeadC - 0.1 * u],
    [0.46 * u, yHeadC + 0.12 * u],
    [0.42 * u, yHeadC + 0.32 * u],
    [0.28 * u, yHeadC + 0.45 * u],
    [0, yHeadC + 0.5 * u],
  ]
  const headSkin = rigid(B.head)
  body.add(revolve(headProfile, 14, 0.92, 1.0), skin, headSkin)
  body.add(sphere(0.075 * u, 0.8, 1.15, 1.2, 0, yHeadC - 0.06 * u, 0.42 * u, 7, 5), skin, headSkin) // nose
  body.add(sphere(0.085 * u, 0.5, 1.0, 0.8, 0.43 * u, yHeadC, 0, 7, 5), skin, headSkin) // ears
  body.add(sphere(0.085 * u, 0.5, 1.0, 0.8, -0.43 * u, yHeadC, 0, 7, 5), skin, headSkin)
  body.add(sphere(0.052 * u, 1, 1, 0.7, 0.16 * u, yHeadC + 0.06 * u, 0.4 * u, 7, 5), dark, headSkin) // eyes
  body.add(sphere(0.052 * u, 1, 1, 0.7, -0.16 * u, yHeadC + 0.06 * u, 0.4 * u, 7, 5), dark, headSkin)
  body.add(box(0.14 * u, 0.028 * u, 0.03 * u, 0.16 * u, yHeadC + 0.17 * u, 0.41 * u), preset.hair === 'bald' ? skinDark : hairC, headSkin) // brows
  body.add(box(0.14 * u, 0.028 * u, 0.03 * u, -0.16 * u, yHeadC + 0.17 * u, 0.41 * u), preset.hair === 'bald' ? skinDark : hairC, headSkin)
  body.add(box(0.15 * u, 0.026 * u, 0.02 * u, 0, yHeadC - 0.22 * u, 0.4 * u), skinDark, headSkin) // mouth

  // ---- hair
  const hairCap = (r: number, thetaLen: number, fringeY: number): BufferGeometry => {
    const g = new SphereGeometry(r, 12, 8, 0, TAU, 0, thetaLen)
    g.scale(0.94, 1.0, 1.04)
    g.translate(0, yHeadC + 0.03 * u, -0.02 * u)
    const pos = g.getAttribute('position')
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i)
      const y = pos.getY(i)
      const z = pos.getZ(i)
      // fringe: lift the low front vertices to a hairline so the forehead shows
      const front = z > 0.12 * u && Math.abs(x) < 0.5 * u
      if (front && y < yHeadC + fringeY) pos.setY(i, yHeadC + fringeY - Math.abs(x) * 0.15)
    }
    pos.needsUpdate = true
    g.computeVertexNormals()
    return g
  }
  const hairDark = shade(hairC, 0.85)
  switch (preset.hair) {
    case 'short':
      body.add(hairCap(0.5 * u, 1.9, 0.22 * u), hairC, headSkin)
      break
    case 'buzz':
      body.add(hairCap(0.475 * u, 1.75, 0.2 * u), hairDark, headSkin)
      break
    case 'curly': {
      body.add(hairCap(0.51 * u, 1.85, 0.18 * u), hairC, headSkin)
      for (let i = 0; i < 9; i++) {
        const a = (i / 9) * TAU
        const r = 0.36 * u + 0.06 * u * hash(i * 3 + 1)
        const y = yHeadC + 0.25 * u + 0.2 * u * hash(i * 5 + 2)
        const cr = (0.15 + 0.06 * hash(i * 7 + 3)) * u
        const front = Math.sin(a) > 0.6
        body.add(sphere(cr, 1, 0.9, 1, Math.cos(a) * r, front ? y + 0.1 * u : y, Math.sin(a) * r * 0.9, 7, 5), i % 2 ? hairC : hairDark, headSkin)
      }
      body.add(sphere(0.2 * u, 1.2, 0.8, 1.2, 0, yHeadC + 0.5 * u, -0.05 * u, 7, 5), hairC, headSkin)
      break
    }
    case 'bun':
      body.add(hairCap(0.5 * u, 2.0, 0.24 * u), hairC, headSkin)
      body.add(sphere(0.18 * u, 1, 0.9, 1, 0, yHeadC + 0.4 * u, -0.34 * u, 8, 6), hairDark, headSkin)
      break
    case 'ponytail': {
      body.add(hairCap(0.5 * u, 2.0, 0.22 * u), hairC, headSkin)
      body.add(sphere(0.13 * u, 1, 1, 1, 0, yHeadC + 0.3 * u, -0.42 * u, 7, 5), hairDark, headSkin)
      const tail = revolve(limbProfile(0.11 * u, 0.05 * u, 0, -0.95 * u, 2, 3), 8)
      tail.rotateX(-0.35)
      tail.translate(0, yHeadC + 0.28 * u, -0.5 * u)
      body.add(tail, hairC, headSkin)
      break
    }
    case 'long': {
      body.add(hairCap(0.5 * u, 1.75, 0.2 * u), hairC, headSkin)
      const curtain: Profile = [
        [0.47 * u, yHeadC + 0.12 * u],
        [0.5 * u, yHeadC - 0.3 * u],
        [0.56 * u, yHeadC - 0.9 * u],
        [0.6 * u, yHeadC - 1.35 * u],
      ]
      body.add(revolve(curtain, 14, 0.94, 1.02, 0.25 * Math.PI, 1.75 * Math.PI), hairC, headSkin)
      break
    }
    case 'braid': {
      body.add(hairCap(0.5 * u, 1.95, 0.24 * u), hairC, headSkin)
      for (let i = 0; i < 5; i++) {
        const t = i / 4
        body.add(sphere((0.11 - 0.015 * i) * u, 1, 1.2, 1, 0, yHeadC + 0.05 * u - 1.1 * u * t, -0.44 * u + 0.1 * u * t, 7, 5), i % 2 ? hairDark : hairC, headSkin)
      }
      break
    }
    case 'undercut':
      body.add(sphere(0.5 * u, 0.78, 0.36, 0.86, 0, yHeadC + 0.4 * u, -0.06 * u, 12, 7), hairC, headSkin)
      body.add(sphere(0.47 * u, 0.9, 0.5, 1.0, 0, yHeadC + 0.16 * u, -0.08 * u, 12, 7), shade(hairC, 0.55).lerp(skin, 0.35), headSkin)
      break
    case 'hat': {
      body.add(hairCap(0.49 * u, 1.55, 0.26 * u), hairC, headSkin)
      const crown = new CylinderGeometry(0.42 * u, 0.5 * u, 0.36 * u, 14)
      crown.translate(0, yHeadC + 0.36 * u, -0.02 * u)
      body.add(crown, accentC, headSkin)
      const brim = new CylinderGeometry(0.62 * u, 0.62 * u, 0.045 * u, 16)
      brim.scale(1, 1, 1.15)
      brim.rotateX(-0.08)
      brim.translate(0, yHeadC + 0.2 * u, 0.12 * u)
      body.add(brim, shade(accentC, 0.85), headSkin)
      break
    }
    case 'bald':
    default:
      break
  }

  // ---- arms
  const rUA = 0.19 * u * limb
  const rEl = 0.155 * u * limb
  const rWr = 0.12 * u * limb
  for (const side of ['L', 'R'] as const) {
    const sgn = side === 'L' ? 1 : -1
    const x = sgn * xShoulder
    const ua = B[`upperArm${side}`]
    const fa = B[`forearm${side}`]
    const hd = B[`hand${side}`]
    const armSkin = chain(
      [
        [ua, yShoulder],
        [fa, yElbow],
        [hd, yWrist],
      ],
      0.15 * u,
    )
    const upper = revolve(limbProfile(rUA * 1.08, rEl, yShoulder, yElbow, 2, 3), 10)
    upper.translate(x, 0, 0)
    body.add(upper, topC, armSkin)
    const fore = revolve(limbProfile(rEl, rWr, yElbow, yWrist, 2, 3), 10)
    fore.translate(x, 0, 0)
    body.add(fore, preset.sleeves === 'long' ? topC : skin, armSkin)
    // hand: flat ellipsoid below the wrist, thumb nub
    body.add(sphere(0.3 * u, 0.22 * limb, 1, 0.42 * limb, x, yWrist - 0.28 * u, 0.02 * u, 7, 5), skin, armSkin)
    body.add(sphere(0.07 * u, 1, 1.3, 1, x, yWrist - 0.16 * u, 0.12 * u, 6, 4), skin, armSkin)
    // short sleeve cuff so the sleeve reads as cloth
    if (preset.sleeves === 'short') body.add(ring(rEl * 1.02, 0.03 * u, x, yElbow + 0.16 * u, 0, 1, 1, 10), shade(topC, 0.85), armSkin)
  }

  // ---- legs
  const rTh = 0.3 * u * limb
  const rKn = 0.2 * u * limb
  const rAn = 0.12 * u * limb
  for (const side of ['L', 'R'] as const) {
    const sgn = side === 'L' ? 1 : -1
    const x = sgn * xHip
    const ul = B[`upperLeg${side}`]
    const ll = B[`lowerLeg${side}`]
    const ft = B[`foot${side}`]
    const legSkin = chain(
      [
        [ul, yHipJ],
        [ll, yKnee],
        [ft, yAnkle],
      ],
      0.15 * u,
    )
    const thigh = revolve(limbProfile(rTh, rKn * 1.02, yHipJ, yKnee, 2, 3), 11)
    thigh.translate(x, 0, 0)
    body.add(thigh, preset.legs === 'bare' ? skin : botC, legSkin)
    // calf with a bulge
    const calf: Profile = [
      [0, yAnkle - rAn],
      [rAn, yAnkle - 0.1 * u],
      [rAn * 1.1, yAnkle + 0.25 * u],
      [rKn * 1.15, yKnee - 1.0 * u],
      [rKn * 1.1, yKnee - 0.45 * u],
      [rKn, yKnee - 0.05 * u],
      [rKn * 0.85, yKnee + 0.12 * u],
      [0, yKnee + 0.2 * u],
    ]
    const shin = revolve(calf, 11)
    shin.translate(x, 0, 0)
    body.add(shin, preset.legs === 'pants' ? botC : skin, legSkin)
    // shoe: a horizontal capsule squashed in y, toe forward (+z), plus a heel block
    const shoe = revolve(limbProfile(0.14 * u * limb, 0.11 * u * limb, 0.36 * u, -0.1 * u, 2, 2), 10)
    shoe.rotateX(-Math.PI / 2) // profile along y → along +z
    shoe.scale(1.05, 0.72, 1)
    shoe.translate(x, 0.1 * u, 0.08 * u)
    body.add(shoe, shoeC, legSkin)
    body.add(box(0.26 * u * limb, 0.2 * u, 0.24 * u, x, 0.1 * u, -0.1 * u), shade(shoeC, 0.8), legSkin)
  }

  // ---- accessories
  const chestSkin = rigid(B.chest)
  for (const acc of preset.accessories) {
    switch (acc) {
      case 'glasses': {
        for (const sgn of [1, -1]) {
          const lens = new TorusGeometry(0.11 * u, 0.02 * u, 4, 12)
          lens.translate(sgn * 0.17 * u, yHeadC + 0.06 * u, 0.45 * u)
          body.add(lens, dark, headSkin)
          body.add(box(0.03 * u, 0.02 * u, 0.42 * u, sgn * 0.42 * u, yHeadC + 0.08 * u, 0.22 * u), dark, headSkin) // arms
        }
        body.add(box(0.12 * u, 0.02 * u, 0.02 * u, 0, yHeadC + 0.08 * u, 0.46 * u), dark, headSkin)
        break
      }
      case 'scarf': {
        body.add(ring(0.27 * u, 0.1 * u, 0, yNeckBase + 0.02 * u, 0.02 * u, 1.1, 0.95), accentC, chain([[B.chest, yChest], [B.neck, yNeckBase]], 0.1 * u))
        body.add(box(0.2 * u, 0.7 * u, 0.08 * u, 0.18 * u, yNeckBase - 0.5 * u, 0.56 * u * sz + 0.02 * u), shade(accentC, 0.9), chestSkin)
        break
      }
      case 'backpack': {
        body.add(box(0.72 * u * shW, 0.95 * u, 0.32 * u, 0, 5.35 * u, -0.72 * u * sz - 0.1 * u), accentC, chestSkin)
        body.add(box(0.5 * u * shW, 0.3 * u, 0.2 * u, 0, 4.85 * u, -0.72 * u * sz - 0.24 * u), shade(accentC, 0.8), chestSkin)
        for (const sgn of [1, -1]) body.add(box(0.08 * u, 0.6 * u, 0.06 * u, sgn * 0.42 * u * shW, 5.55 * u, 0.66 * u * sz), shade(accentC, 0.7), chestSkin)
        break
      }
      case 'toolbelt': {
        const belt = new Color('#4a3322')
        body.add(ring(0.77 * u * hipW, 0.06 * u, 0, 3.98 * u, 0, 1.03, sz * 1.05), belt, rigid(B.hips))
        for (const sgn of [1, -1]) body.add(box(0.16 * u, 0.26 * u, 0.2 * u, sgn * 0.78 * u * hipW, 3.85 * u, 0.15 * u), shade(belt, 0.85), rigid(B.hips))
        body.add(box(0.06 * u, 0.3 * u, 0.06 * u, 0.6 * u * hipW, 3.75 * u, 0.4 * u * sz), new Color('#9aa0a8'), rigid(B.hips)) // a hanging tool
        break
      }
      case 'bracelet':
        body.add(ring(rWr * 1.15, 0.03 * u, xShoulder, yWrist + 0.06 * u, 0, 1, 1, 10), accentC, chain([[B.forearmL, yElbow], [B.handL, yWrist]], 0.1 * u))
        break
      case 'necklace': {
        const gold = new Color('#e0b45a')
        const chainRing = new TorusGeometry(0.24 * u, 0.018 * u, 3, 16)
        chainRing.rotateX(Math.PI / 2 + 0.35)
        chainRing.translate(0, yNeckBase - 0.1 * u, 0.12 * u)
        body.add(chainRing, gold, chestSkin)
        body.add(sphere(0.05 * u, 1, 1.3, 0.7, 0, yNeckBase - 0.34 * u, 0.5 * u * sz + 0.03 * u, 6, 5), gold, chestSkin)
        break
      }
    }
  }

  // ---- trim (tier-tinted): headband, belt, wrist band
  for (const part of preset.trim) {
    switch (part) {
      case 'headband':
        if (preset.hair === 'hat') break
        trim.add(ring(0.475 * u, 0.045 * u, 0, yHeadC + 0.2 * u, -0.02 * u, 0.94, 1.04, 16), skin, headSkin)
        break
      case 'belt':
        trim.add(ring(0.665 * u * waistW, 0.06 * u, 0, 4.42 * u, 0, 1.05 * torsoScale, sz * 1.08 * torsoScale, 16), skin, torsoSkin)
        break
      case 'wrist':
        trim.add(ring(rWr * 1.2, 0.04 * u, -xShoulder, yWrist + 0.05 * u, 0, 1, 1, 10), skin, chain([[B.forearmR, yElbow], [B.handR, yWrist]], 0.1 * u))
        break
    }
  }

  // ---- assemble
  const root = new Group()
  root.name = 'humanoid:' + preset.name
  root.add(hips)
  root.updateMatrixWorld(true)
  const skeleton = new Skeleton(boneList)
  const identity = new Matrix4()

  const bodyMat = new MeshStandardMaterial({ vertexColors: true, roughness: 0.8, metalness: 0.02 })
  const trimMat = new MeshStandardMaterial({ color: '#8b93a7', emissive: '#8b93a7', emissiveIntensity: 0.45, roughness: 0.45, metalness: 0.1 })
  const bodyMesh = new SkinnedMesh(body.build(), bodyMat)
  bodyMesh.name = 'body'
  bodyMesh.frustumCulled = false
  root.add(bodyMesh)
  bodyMesh.bind(skeleton, identity)
  const trimMesh = new SkinnedMesh(trim.build(), trimMat)
  trimMesh.name = 'trim'
  trimMesh.frustumCulled = false
  root.add(trimMesh)
  trimMesh.bind(skeleton, identity)

  return {
    root,
    skinned: [bodyMesh, trimMesh],
    bones,
    boneList,
    skeleton,
    height: yTop,
    body: bodyMesh,
    trim: trimMesh,
    materials: { body: bodyMat, trim: trimMat },
    dims: D,
    dispose() {
      bodyMesh.geometry.dispose()
      trimMesh.geometry.dispose()
      bodyMat.dispose()
      trimMat.dispose()
      skeleton.dispose()
    },
  }
}
