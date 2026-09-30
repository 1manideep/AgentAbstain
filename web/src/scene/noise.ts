/**
 * Seeded 2-D simplex noise and fractal Brownian motion. Deterministic for a
 * given seed (the run's hello.config.seed), no dependency, no allocation in
 * `noise2`. Used for the terrain heightmap, vertex colour variation, prop
 * placement and the scrolling cloud shadow.
 */

const F2 = 0.5 * (Math.sqrt(3) - 1)
const G2 = (3 - Math.sqrt(3)) / 6
const GRAD = [
  [1, 1],
  [-1, 1],
  [1, -1],
  [-1, -1],
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
] as const

/** Small fast PRNG (mulberry32) — the same one the fixture generator uses. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export class Noise2D {
  private perm = new Uint8Array(512)
  private permMod8 = new Uint8Array(512)

  constructor(seed: number) {
    const rand = mulberry32(seed)
    const p = new Uint8Array(256)
    for (let i = 0; i < 256; i++) p[i] = i
    for (let i = 255; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1))
      const t = p[i]!
      p[i] = p[j]!
      p[j] = t
    }
    for (let i = 0; i < 512; i++) {
      this.perm[i] = p[i & 255]!
      this.permMod8[i] = this.perm[i]! & 7
    }
  }

  /** Simplex noise in [-1, 1]. */
  noise2(xin: number, yin: number): number {
    const s = (xin + yin) * F2
    const i = Math.floor(xin + s)
    const j = Math.floor(yin + s)
    const t = (i + j) * G2
    const x0 = xin - (i - t)
    const y0 = yin - (j - t)
    let i1: number
    let j1: number
    if (x0 > y0) {
      i1 = 1
      j1 = 0
    } else {
      i1 = 0
      j1 = 1
    }
    const x1 = x0 - i1 + G2
    const y1 = y0 - j1 + G2
    const x2 = x0 - 1 + 2 * G2
    const y2 = y0 - 1 + 2 * G2
    const ii = i & 255
    const jj = j & 255
    let n0 = 0
    let n1 = 0
    let n2 = 0
    let t0 = 0.5 - x0 * x0 - y0 * y0
    if (t0 >= 0) {
      const g = GRAD[this.permMod8[ii + this.perm[jj]!]!]!
      t0 *= t0
      n0 = t0 * t0 * (g[0] * x0 + g[1] * y0)
    }
    let t1 = 0.5 - x1 * x1 - y1 * y1
    if (t1 >= 0) {
      const g = GRAD[this.permMod8[ii + i1 + this.perm[jj + j1]!]!]!
      t1 *= t1
      n1 = t1 * t1 * (g[0] * x1 + g[1] * y1)
    }
    let t2 = 0.5 - x2 * x2 - y2 * y2
    if (t2 >= 0) {
      const g = GRAD[this.permMod8[ii + 1 + this.perm[jj + 1]!]!]!
      t2 *= t2
      n2 = t2 * t2 * (g[0] * x2 + g[1] * y2)
    }
    return 70 * (n0 + n1 + n2)
  }

  /** Fractal Brownian motion in roughly [-1, 1]. */
  fbm(x: number, y: number, octaves = 5, lacunarity = 2.0, gain = 0.5): number {
    let amp = 1
    let freq = 1
    let sum = 0
    let norm = 0
    for (let o = 0; o < octaves; o++) {
      sum += amp * this.noise2(x * freq, y * freq)
      norm += amp
      amp *= gain
      freq *= lacunarity
    }
    return sum / norm
  }
}
