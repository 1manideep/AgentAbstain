/**
 * Seeded terrain: a heightmap over the world square plus a margin, with a
 * flat basin around every resource node and flattened paths between nodes so
 * agents never float or sink. The same `heightAt(x, z)` is sampled on the CPU
 * for agent/node/gadget/prop placement and on the grid for the mesh.
 */
import { BufferAttribute, BufferGeometry, Color } from 'three'
import { mulberry32, Noise2D } from './noise'

export const WATER_LEVEL = 0
export const BASIN_LEVEL = 0.35
export const MAX_HILL = 2.5
export const MIN_HEIGHT = -0.9
export const BASIN_RADIUS = 4.2
export const BASIN_FALLOFF = 3.5
export const PATH_HALF_WIDTH = 1.4
export const PATH_FALLOFF = 2.2

export interface TerrainNode {
  id: string
  x: number
  y: number
}

interface Segment {
  ax: number
  az: number
  bx: number
  bz: number
  len2: number
}

function smoothstep(e0: number, e1: number, x: number): number {
  const t = (x - e0) / (e1 - e0)
  const k = t < 0 ? 0 : t > 1 ? 1 : t
  return k * k * (3 - 2 * k)
}

export class Terrain {
  readonly seed: number
  readonly size: number
  readonly half: number
  /** Geometry extent (half width) including the margin beyond the world. */
  readonly extent: number
  readonly nodes: TerrainNode[]
  readonly segments: Segment[]
  private noise: Noise2D
  private colorNoise: Noise2D
  private freq: number

  constructor(seed: number, size: number, nodes: TerrainNode[]) {
    this.seed = seed
    this.size = size
    this.half = size / 2
    this.extent = size * 1.1
    this.nodes = nodes.map((n) => ({ id: n.id, x: n.x, y: n.y }))
    this.noise = new Noise2D(seed)
    this.colorNoise = new Noise2D(seed ^ 0x9e3779b9)
    this.freq = 1 / (size * 0.42)
    this.segments = Terrain.buildPaths(this.nodes)
  }

  /** Ring of nodes by angle plus each node's nearest neighbour: every basin is reachable. */
  private static buildPaths(nodes: TerrainNode[]): Segment[] {
    const segs: Segment[] = []
    const key = (a: number, b: number) => (a < b ? `${a}-${b}` : `${b}-${a}`)
    const seen = new Set<string>()
    const add = (i: number, j: number) => {
      if (i === j) return
      const k = key(i, j)
      if (seen.has(k)) return
      seen.add(k)
      const a = nodes[i]!
      const b = nodes[j]!
      segs.push({ ax: a.x, az: a.y, bx: b.x, bz: b.y, len2: (b.x - a.x) ** 2 + (b.y - a.y) ** 2 })
    }
    if (nodes.length < 2) return segs
    const order = nodes.map((n, i) => ({ i, a: Math.atan2(n.y, n.x) })).sort((p, q) => p.a - q.a)
    for (let k = 0; k < order.length; k++) add(order[k]!.i, order[(k + 1) % order.length]!.i)
    for (let i = 0; i < nodes.length; i++) {
      let best = -1
      let bd = Infinity
      for (let j = 0; j < nodes.length; j++) {
        if (i === j) continue
        const d = (nodes[i]!.x - nodes[j]!.x) ** 2 + (nodes[i]!.y - nodes[j]!.y) ** 2
        if (d < bd) {
          bd = d
          best = j
        }
      }
      if (best >= 0) add(i, best)
    }
    return segs
  }

  /** Raw hills before basins and paths: MIN_HEIGHT..MAX_HILL, gentle. */
  rawHeight(x: number, z: number): number {
    const n = this.noise.fbm(x * this.freq + 3.1, z * this.freq - 1.7, 5, 2.0, 0.5) // ≈ [-0.75, 0.75]
    const k = (n + 0.75) / 1.5
    const shaped = k < 0 ? 0 : k > 1 ? 1 : k
    // bias toward low ground so lakes appear only in the deepest valleys
    return MIN_HEIGHT + (MAX_HILL - MIN_HEIGHT) * (shaped * shaped * 0.85 + shaped * 0.15)
  }

  /** How strongly this point is flattened toward the basin level: 0 = natural, 1 = fully flat. */
  flatness(x: number, z: number): number {
    let f = 0
    for (const n of this.nodes) {
      const d = Math.hypot(x - n.x, z - n.y)
      const k = 1 - smoothstep(BASIN_RADIUS, BASIN_RADIUS + BASIN_FALLOFF, d)
      if (k > f) f = k
    }
    for (const s of this.segments) {
      const d = this.distToSegment(x, z, s)
      const k = 1 - smoothstep(PATH_HALF_WIDTH, PATH_HALF_WIDTH + PATH_FALLOFF, d)
      if (k > f) f = k
    }
    return f
  }

  private distToSegment(x: number, z: number, s: Segment): number {
    if (s.len2 === 0) return Math.hypot(x - s.ax, z - s.az)
    let t = ((x - s.ax) * (s.bx - s.ax) + (z - s.az) * (s.bz - s.az)) / s.len2
    t = t < 0 ? 0 : t > 1 ? 1 : t
    return Math.hypot(x - (s.ax + t * (s.bx - s.ax)), z - (s.az + t * (s.bz - s.az)))
  }

  /** Final terrain height at world (x, z). Deterministic; no allocation. */
  heightAt(x: number, z: number): number {
    const raw = this.rawHeight(x, z)
    const f = this.flatness(x, z)
    if (f <= 0) return raw
    // paths and basins sit at the basin level with a whisper of the underlying relief
    const flat = BASIN_LEVEL + (raw - BASIN_LEVEL) * 0.06
    return raw + (flat - raw) * f
  }

  /** Where agents stand: never below wading depth in a lake. */
  standHeight(x: number, z: number): number {
    const h = this.heightAt(x, z)
    return h < WATER_LEVEL - 0.55 ? WATER_LEVEL - 0.55 : h
  }

  /** Small deterministic colour/placement noise in [-1, 1]. */
  detail(x: number, z: number): number {
    return this.colorNoise.noise2(x * 0.35, z * 0.35)
  }

  /** True inside a node basin or on a path (props keep out). */
  isReserved(x: number, z: number, pad = 1.0): boolean {
    for (const n of this.nodes) if (Math.hypot(x - n.x, z - n.y) < BASIN_RADIUS + pad) return true
    for (const s of this.segments) if (this.distToSegment(x, z, s) < PATH_HALF_WIDTH + pad) return true
    return false
  }

  /**
   * Builds the terrain mesh: positions, normals, vertex colours (grass / dry /
   * rock by height and slope) and an `aGrass` attribute for scarcity tinting.
   */
  buildGeometry(res = 128): BufferGeometry {
    const n = res + 1
    const ext = this.extent
    const positions = new Float32Array(n * n * 3)
    const colors = new Float32Array(n * n * 3)
    const grass = new Float32Array(n * n)
    const heights = new Float32Array(n * n)
    for (let j = 0; j < n; j++) {
      for (let i = 0; i < n; i++) {
        const x = -ext + (2 * ext * i) / res
        const z = -ext + (2 * ext * j) / res
        const h = this.heightAt(x, z)
        const idx = j * n + i
        heights[idx] = h
        positions[idx * 3] = x
        positions[idx * 3 + 1] = h
        positions[idx * 3 + 2] = z
      }
    }
    const indices = new Uint32Array(res * res * 6)
    let k = 0
    for (let j = 0; j < res; j++) {
      for (let i = 0; i < res; i++) {
        const a = j * n + i
        const b = a + 1
        const c = a + n
        const d = c + 1
        indices[k++] = a
        indices[k++] = c
        indices[k++] = b
        indices[k++] = b
        indices[k++] = c
        indices[k++] = d
      }
    }
    const geom = new BufferGeometry()
    geom.setIndex(new BufferAttribute(indices, 1))
    geom.setAttribute('position', new BufferAttribute(positions, 3))
    geom.computeVertexNormals()
    const normals = geom.getAttribute('normal') as BufferAttribute
    const GRASS = new Color('#3f7a3a')
    const GRASS2 = new Color('#5c8f3e')
    const DRY = new Color('#8a7b4f')
    const ROCK = new Color('#6b6d75')
    const SAND = new Color('#7a7052')
    const MUD = new Color('#3c4a3a')
    const c = new Color()
    for (let idx = 0; idx < n * n; idx++) {
      const x = positions[idx * 3]!
      const z = positions[idx * 3 + 2]!
      const h = heights[idx]!
      const ny = normals.getY(idx)
      const slope = 1 - ny // 0 flat .. 1 vertical
      const v = this.detail(x, z)
      const hk = (h - MIN_HEIGHT) / (MAX_HILL - MIN_HEIGHT)
      // base by height: mud below water, sand at the shore, grass on low ground, dry ground higher
      if (h < WATER_LEVEL - 0.15) c.copy(MUD)
      else if (h < WATER_LEVEL + 0.25) c.copy(SAND).lerp(GRASS, smoothstep(WATER_LEVEL - 0.15, WATER_LEVEL + 0.25, h))
      else c.copy(GRASS).lerp(GRASS2, 0.5 + 0.5 * v).lerp(DRY, smoothstep(0.45, 0.95, hk + 0.15 * v))
      // rock on steep slopes and the highest crowns
      const rockAmt = Math.max(smoothstep(0.22, 0.5, slope), smoothstep(0.88, 1.0, hk))
      c.lerp(ROCK, rockAmt)
      // gentle per-vertex variation so the lighting reads as ground, not plastic
      const shade = 0.92 + 0.08 * this.detail(z * 1.7, x * 1.3)
      colors[idx * 3] = c.r * shade
      colors[idx * 3 + 1] = c.g * shade
      colors[idx * 3 + 2] = c.b * shade
      const g = h > WATER_LEVEL + 0.1 ? 1 - Math.max(rockAmt, smoothstep(0.5, 0.95, hk + 0.15 * v)) : 0
      grass[idx] = g < 0 ? 0 : g
    }
    geom.setAttribute('color', new BufferAttribute(colors, 3))
    geom.setAttribute('aGrass', new BufferAttribute(grass, 1))
    geom.computeBoundingSphere()
    return geom
  }

  /**
   * Poisson-like placement: jittered grid candidates, rejected inside basins,
   * on paths, under water, beyond the extent or (for `maxSlope`) on steep ground.
   */
  scatter(opts: { spacing: number; jitter?: number; maxSlope?: number; minHeight?: number; salt?: number; limit?: number; margin?: number }): Array<{ x: number; z: number; y: number; r: number }> {
    const rand = mulberry32((this.seed ^ ((opts.salt ?? 0) * 2654435761)) >>> 0)
    const out: Array<{ x: number; z: number; y: number; r: number }> = []
    const spacing = opts.spacing
    const jitter = opts.jitter ?? 0.45
    const maxSlope = opts.maxSlope ?? 0.45
    const minHeight = opts.minHeight ?? WATER_LEVEL + 0.15
    const limit = opts.limit ?? 1000
    const reach = this.extent - (opts.margin ?? 2)
    const cells = Math.floor((2 * reach) / spacing)
    const start = -reach + spacing / 2
    for (let j = 0; j < cells; j++) {
      for (let i = 0; i < cells; i++) {
        const x = start + i * spacing + (rand() - 0.5) * 2 * jitter * spacing
        const z = start + j * spacing + (rand() - 0.5) * 2 * jitter * spacing
        const r = rand()
        if (Math.abs(x) > reach || Math.abs(z) > reach) continue
        if (this.isReserved(x, z, 0.8)) continue
        const h = this.heightAt(x, z)
        if (h < minHeight) continue
        const dx = this.heightAt(x + 0.6, z) - this.heightAt(x - 0.6, z)
        const dz = this.heightAt(x, z + 0.6) - this.heightAt(x, z - 0.6)
        const slope = Math.hypot(dx, dz) / 1.2
        if (slope > maxSlope) continue
        out.push({ x, z, y: h, r })
        if (out.length >= limit) return out
      }
    }
    return out
  }
}

/** Live terrain shared with the driver and scene systems (null until nodes are known). */
export const terrainState: { terrain: Terrain | null; rev: number; listeners: Set<() => void> } = {
  terrain: null,
  rev: 0,
  listeners: new Set(),
}

export function setTerrain(t: Terrain | null): void {
  terrainState.terrain = t
  terrainState.rev++
  for (const fn of terrainState.listeners) fn()
}

export function heightAt(x: number, z: number): number {
  const t = terrainState.terrain
  return t ? t.standHeight(x, z) : 0
}
