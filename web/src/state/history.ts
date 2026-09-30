/**
 * History — the per-frame world state buffer that lives outside React (DESIGN §16).
 *
 * Snapshots are parsed once on arrival into typed-array frames with stable agent
 * slots. A virtual timeline (`vts`) absorbs irregular server intervals: each
 * step is clamped to 3× an EMA of recent steps, so a 20 s pause becomes a short
 * hop instead of a long freeze. The render clock chases `newestVts − 1.25·ema`
 * with a ±15 % rate control and snaps when it falls more than 3·ema behind.
 *
 * `sample(renderVts, out)` is the hot path: no allocation, binary search for the
 * bracketing frames, linear position, shortest-arc heading, linear stress and
 * balance, births scale in over 400 ms, deaths hold position and fade for 1 s.
 */

import type { AgentStatus, AnimState, SnapshotMsg } from '../protocol'

export const SLOT_STRIDE = 6 // x, y, heading, stress, t_eff, balance
export const FLAG_STRIDE = 4 // asleep, status, degenerate, anim
export const NODE_STRIDE = 5 // x, y, stock, capacity, stock_delta

export const STATUS_ABSENT = 0
export const STATUS_ALIVE = 1
export const STATUS_BANKRUPT = 2
export const STATUS_ARCHIVED = 3

export const ANIM_IDLE = 0
export const ANIM_WALK = 1
export const ANIM_SLEEP = 2
export const ANIM_DEGENERATE = 3
export const ANIM_DEAD = 4

export const BIRTH_MS = 400
export const TOMBSTONE_MS = 1000
export const EMA_MIN = 200
export const EMA_MAX = 60000
export const EMA_ALPHA = 0.2
export const CLAMP_FACTOR = 3
export const DELAY_FACTOR = 1.25
export const EASE_MS = 500

export type PlaybackMode = 'live' | 'scrub'
export type PlaybackSpeed = 0 | 0.25 | 0.5 | 1 | 2 | 4
export const SPEEDS: readonly PlaybackSpeed[] = [0, 0.25, 0.5, 1, 2, 4]

export function statusCode(s: AgentStatus | string): number {
  switch (s) {
    case 'alive':
      return STATUS_ALIVE
    case 'bankrupt':
      return STATUS_BANKRUPT
    case 'archived':
      return STATUS_ARCHIVED
    default:
      return STATUS_ABSENT
  }
}

export function animCode(a: AnimState | string): number {
  switch (a) {
    case 'walk':
      return ANIM_WALK
    case 'sleep':
      return ANIM_SLEEP
    case 'degenerate':
      return ANIM_DEGENERATE
    case 'dead':
      return ANIM_DEAD
    default:
      return ANIM_IDLE
  }
}

export interface FrameWorld {
  weather: number
  scarcity: number
  paused: boolean
  spendToday: number
  spendTotal: number
  spawnPool: number
  population: number
  gadgetsRev: number
  tasksRev: number
  chronicleRev: number
  epochs: SnapshotMsg['epochs']
}

export interface Frame {
  /** History-local monotonic sequence (not the server seq). */
  seq: number
  tick: number
  day: number
  tickOfDay: number
  ts: number
  vts: number
  capacity: number
  nodeCapacity: number
  ids: string[]
  data: Float32Array
  flags: Uint8Array
  nodeIds: string[]
  nodes: Float32Array
  world: FrameWorld
}

/** Preallocated output of `History.sample`. Reused every frame; never allocate inside sample. */
export class SampleOut {
  capacity: number
  nodeCapacity: number
  count = 0
  nodeCount = 0
  tick = 0
  day = 0
  tickOfDay = 0
  /** Ticks of the bracketing frames (older, newer). */
  tickA = 0
  tickB = 0
  frac = 0
  weather = 0
  scarcity = 1
  frameA: Frame | null = null
  frameB: Frame | null = null

  ids: string[]
  x: Float32Array
  y: Float32Array
  heading: Float32Array
  stress: Float32Array
  tEff: Float32Array
  balance: Float32Array
  /** Velocity in world units per virtual second. */
  vx: Float32Array
  vy: Float32Array
  /** Birth scale-in 0..1. */
  scale: Float32Array
  /** Tombstone fade 1..0. */
  alpha: Float32Array
  present: Uint8Array
  dead: Uint8Array
  status: Uint8Array
  asleep: Uint8Array
  degenerate: Uint8Array
  anim: Uint8Array

  nodeIds: string[]
  nx: Float32Array
  ny: Float32Array
  nstock: Float32Array
  ncap: Float32Array
  ndelta: Float32Array
  nodePresent: Uint8Array

  constructor(capacity: number, nodeCapacity: number) {
    this.capacity = capacity
    this.nodeCapacity = nodeCapacity
    this.ids = new Array<string>(capacity).fill('')
    this.x = new Float32Array(capacity)
    this.y = new Float32Array(capacity)
    this.heading = new Float32Array(capacity)
    this.stress = new Float32Array(capacity)
    this.tEff = new Float32Array(capacity)
    this.balance = new Float32Array(capacity)
    this.vx = new Float32Array(capacity)
    this.vy = new Float32Array(capacity)
    this.scale = new Float32Array(capacity)
    this.alpha = new Float32Array(capacity)
    this.present = new Uint8Array(capacity)
    this.dead = new Uint8Array(capacity)
    this.status = new Uint8Array(capacity)
    this.asleep = new Uint8Array(capacity)
    this.degenerate = new Uint8Array(capacity)
    this.anim = new Uint8Array(capacity)
    this.nodeIds = new Array<string>(nodeCapacity).fill('')
    this.nx = new Float32Array(nodeCapacity)
    this.ny = new Float32Array(nodeCapacity)
    this.nstock = new Float32Array(nodeCapacity)
    this.ncap = new Float32Array(nodeCapacity)
    this.ndelta = new Float32Array(nodeCapacity)
    this.nodePresent = new Uint8Array(nodeCapacity)
  }
}

export interface PlaybackState {
  mode: PlaybackMode
  speed: PlaybackSpeed
  renderVts: number
  /** Rate multiplier chosen by the lag controller on the last advance. */
  rate: number
  easing: boolean
  easeFrom: number
  easeT: number
  snaps: number
}

function shortestArc(a: number, b: number, t: number): number {
  let d = b - a
  while (d > Math.PI) d -= 2 * Math.PI
  while (d < -Math.PI) d += 2 * Math.PI
  return a + d * t
}

/** Linear interpolation that tolerates a missing (NaN) endpoint, e.g. t_eff when an agent made no call. */
function lerpNaN(a: number, b: number, t: number): number {
  if (a !== a) return b
  if (b !== b) return a
  return a + (b - a) * t
}

function smoothstep(k: number): number {
  const x = k < 0 ? 0 : k > 1 ? 1 : k
  return x * x * (3 - 2 * x)
}

export class History {
  readonly ringCapacity: number
  private frames: (Frame | null)[]
  private head = 0
  private _count = 0
  private frameSeq = 0

  ema = 1000
  newestVts = 0
  oldestVts = 0
  newestTs = 0

  capacity: number
  readonly agentIndex = new Map<string, number>()
  private freeSlots: number[] = []
  private slotLastSeenSeq: Int32Array
  private slotBornVts: Float64Array
  private slotDiedVts: Float64Array
  private slotLast: Float32Array
  private slotDeadId: string[]

  nodeCapacity: number
  readonly nodeIndex = new Map<string, number>()

  readonly playback: PlaybackState = {
    mode: 'live',
    speed: 1,
    renderVts: 0,
    rate: 1,
    easing: false,
    easeFrom: 0,
    easeT: 0,
    snaps: 0,
  }

  private capacityListeners = new Set<(cap: number, nodeCap: number) => void>()
  private lastSpeed: PlaybackSpeed = 1

  constructor(ringCapacity = 600, capacity = 16, nodeCapacity = 8) {
    this.ringCapacity = ringCapacity
    this.frames = new Array<Frame | null>(ringCapacity).fill(null)
    this.capacity = capacity
    this.nodeCapacity = nodeCapacity
    this.slotLastSeenSeq = new Int32Array(capacity).fill(-1)
    this.slotBornVts = new Float64Array(capacity).fill(Number.POSITIVE_INFINITY)
    this.slotDiedVts = new Float64Array(capacity).fill(Number.POSITIVE_INFINITY)
    this.slotLast = new Float32Array(capacity * SLOT_STRIDE)
    this.slotDeadId = new Array<string>(capacity).fill('')
  }

  // ------------------------------------------------------------ ring access

  get count(): number {
    return this._count
  }

  frameAt(i: number): Frame {
    const f = this.frames[(this.head + i) % this.ringCapacity]
    if (!f) throw new Error(`history: no frame at ${i}`)
    return f
  }

  newest(): Frame | null {
    return this._count === 0 ? null : this.frameAt(this._count - 1)
  }

  oldest(): Frame | null {
    return this._count === 0 ? null : this.frameAt(0)
  }

  onCapacityChange(fn: (cap: number, nodeCap: number) => void): () => void {
    this.capacityListeners.add(fn)
    return () => this.capacityListeners.delete(fn)
  }

  /** Called on hello: size slots for the run. Never shrinks. */
  configure(populationCap: number, nodeCount: number): void {
    const wantSlots = Math.max(16, populationCap * 3)
    const wantNodes = Math.max(8, nodeCount)
    if (wantSlots > this.capacity) this.growSlots(wantSlots)
    if (wantNodes > this.nodeCapacity) {
      this.nodeCapacity = wantNodes
      this.emitCapacity()
    }
  }

  reset(): void {
    this.frames.fill(null)
    this.head = 0
    this._count = 0
    this.frameSeq = 0
    this.ema = 1000
    this.newestVts = 0
    this.oldestVts = 0
    this.newestTs = 0
    this.agentIndex.clear()
    this.nodeIndex.clear()
    this.freeSlots = []
    this.slotLastSeenSeq.fill(-1)
    this.slotBornVts.fill(Number.POSITIVE_INFINITY)
    this.slotDiedVts.fill(Number.POSITIVE_INFINITY)
    this.slotLast.fill(0)
    this.slotDeadId.fill('')
    const p = this.playback
    p.mode = 'live'
    p.speed = 1
    p.renderVts = 0
    p.rate = 1
    p.easing = false
    p.snaps = 0
  }

  private emitCapacity(): void {
    for (const fn of this.capacityListeners) fn(this.capacity, this.nodeCapacity)
  }

  private growSlots(newCap: number): void {
    const old = this.capacity
    const lastSeen = new Int32Array(newCap).fill(-1)
    lastSeen.set(this.slotLastSeenSeq)
    const born = new Float64Array(newCap).fill(Number.POSITIVE_INFINITY)
    born.set(this.slotBornVts)
    const died = new Float64Array(newCap).fill(Number.POSITIVE_INFINITY)
    died.set(this.slotDiedVts)
    const last = new Float32Array(newCap * SLOT_STRIDE)
    last.set(this.slotLast)
    const deadId = new Array<string>(newCap).fill('')
    for (let i = 0; i < old; i++) deadId[i] = this.slotDeadId[i]!
    this.slotLastSeenSeq = lastSeen
    this.slotBornVts = born
    this.slotDiedVts = died
    this.slotLast = last
    this.slotDeadId = deadId
    this.capacity = newCap
    this.emitCapacity()
  }

  private oldestRetainedSeq(): number {
    const o = this.oldest()
    return o ? o.seq : 0
  }

  private claimSlot(id: string, s: number): number {
    this.agentIndex.set(id, s)
    // Mark it taken for the frame about to be written so two new ids never share a slot.
    this.slotLastSeenSeq[s] = this.frameSeq
    return s
  }

  private allocSlot(id: string): number {
    const free = this.freeSlots.pop()
    if (free !== undefined) return this.claimSlot(id, free)
    // Recycle a slot whose last occupant is no longer referenced by any retained frame.
    const floor = this.oldestRetainedSeq()
    for (let s = 0; s < this.capacity; s++) {
      const seen = this.slotLastSeenSeq[s]!
      if (seen < 0) return this.claimSlot(id, s)
      if (seen < floor) {
        for (const [oid, os] of this.agentIndex) {
          if (os === s) {
            this.agentIndex.delete(oid)
            break
          }
        }
        this.slotBornVts[s] = Number.POSITIVE_INFINITY
        this.slotDiedVts[s] = Number.POSITIVE_INFINITY
        this.slotDeadId[s] = ''
        return this.claimSlot(id, s)
      }
    }
    const s = this.capacity
    this.growSlots(this.capacity * 2)
    return this.claimSlot(id, s)
  }

  private allocNode(id: string): number {
    const existing = this.nodeIndex.get(id)
    if (existing !== undefined) return existing
    const s = this.nodeIndex.size
    if (s >= this.nodeCapacity) {
      this.nodeCapacity = Math.max(this.nodeCapacity * 2, s + 1)
      this.emitCapacity()
    }
    this.nodeIndex.set(id, s)
    return s
  }

  // ------------------------------------------------------------ ingest

  /** Parse a snapshot into a frame and append it. Returns the frame. */
  push(snap: SnapshotMsg): Frame {
    const prev = this.newest()
    // Virtual timeline: clamp each step so pauses never freeze interpolation.
    let vts: number
    if (!prev) {
      vts = 0
    } else {
      const rawDt = snap.ts_ms - prev.ts
      const dt = rawDt > 1 ? rawDt : 1
      const clamped = Math.min(dt, CLAMP_FACTOR * this.ema)
      vts = prev.vts + clamped
      const ema = (1 - EMA_ALPHA) * this.ema + EMA_ALPHA * clamped
      this.ema = ema < EMA_MIN ? EMA_MIN : ema > EMA_MAX ? EMA_MAX : ema
    }

    // Make sure every id has a slot before we size the arrays.
    for (const a of snap.agents) if (!this.agentIndex.has(a.id)) this.allocSlot(a.id)
    for (const n of snap.nodes) this.allocNode(n.id)

    const cap = this.capacity
    const ncap = this.nodeCapacity
    const seq = this.frameSeq++
    const frame: Frame = {
      seq,
      tick: snap.tick,
      day: snap.day,
      tickOfDay: snap.tick_of_day,
      ts: snap.ts_ms,
      vts,
      capacity: cap,
      nodeCapacity: ncap,
      ids: new Array<string>(cap).fill(''),
      data: new Float32Array(cap * SLOT_STRIDE),
      flags: new Uint8Array(cap * FLAG_STRIDE),
      nodeIds: new Array<string>(ncap).fill(''),
      nodes: new Float32Array(ncap * NODE_STRIDE),
      world: {
        weather: snap.weather,
        scarcity: snap.scarcity,
        paused: snap.paused,
        spendToday: snap.spend_today_usd,
        spendTotal: snap.spend_total_usd,
        spawnPool: snap.spawn_pool_usd,
        population: snap.population,
        gadgetsRev: snap.gadgets_rev,
        tasksRev: snap.tasks_rev,
        chronicleRev: snap.chronicle_rev,
        epochs: snap.epochs,
      },
    }

    for (const a of snap.agents) {
      const s = this.agentIndex.get(a.id)!
      frame.ids[s] = a.id
      const d = s * SLOT_STRIDE
      frame.data[d] = a.x
      frame.data[d + 1] = a.y
      frame.data[d + 2] = a.heading
      frame.data[d + 3] = a.stress
      frame.data[d + 4] = typeof a.t_eff === 'number' ? a.t_eff : Number.NaN
      frame.data[d + 5] = a.balance_usd
      const f = s * FLAG_STRIDE
      frame.flags[f] = a.asleep ? 1 : 0
      frame.flags[f + 1] = statusCode(a.status)
      frame.flags[f + 2] = a.degenerate ? 1 : 0
      frame.flags[f + 3] = animCode(a.anim)
      this.slotLastSeenSeq[s] = seq
    }
    for (const n of snap.nodes) {
      const s = this.nodeIndex.get(n.id)!
      frame.nodeIds[s] = n.id
      const d = s * NODE_STRIDE
      frame.nodes[d] = n.x
      frame.nodes[d + 1] = n.y
      frame.nodes[d + 2] = n.stock
      frame.nodes[d + 3] = n.capacity
      frame.nodes[d + 4] = n.stock_delta
    }

    // Lifecycle bookkeeping (births, deaths) against the previous newest frame.
    for (let s = 0; s < cap; s++) {
      const idNew = frame.ids[s]!
      const aliveNew = idNew !== '' && frame.flags[s * FLAG_STRIDE + 1] === STATUS_ALIVE
      const idPrev = prev && s < prev.capacity ? prev.ids[s]! : ''
      const alivePrev = idPrev !== '' && prev!.flags[s * FLAG_STRIDE + 1] === STATUS_ALIVE
      if (aliveNew && (!alivePrev || idPrev !== idNew)) {
        this.slotBornVts[s] = vts
        this.slotDiedVts[s] = Number.POSITIVE_INFINITY
      }
      if (alivePrev && !aliveNew) {
        this.slotDiedVts[s] = vts
        this.slotDeadId[s] = idPrev
        const src = idNew === idPrev ? frame : prev!
        const d = s * SLOT_STRIDE
        for (let k = 0; k < SLOT_STRIDE; k++) this.slotLast[d + k] = src.data[d + k]!
      }
    }

    // Append to the ring (evict oldest when full).
    if (this._count === this.ringCapacity) {
      this.frames[this.head] = frame
      this.head = (this.head + 1) % this.ringCapacity
    } else {
      this.frames[(this.head + this._count) % this.ringCapacity] = frame
      this._count++
    }
    this.newestVts = vts
    this.newestTs = snap.ts_ms
    this.oldestVts = this.frameAt(0).vts
    if (this._count === 1) this.playback.renderVts = vts
    return frame
  }

  // ------------------------------------------------------------ lookup

  /** Index of the last frame with vts <= v (0 when before the first frame). */
  indexAtVts(v: number): number {
    let lo = 0
    let hi = this._count - 1
    if (hi < 0) return -1
    if (v <= this.frameAt(0).vts) return 0
    if (v >= this.frameAt(hi).vts) return hi
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (this.frameAt(mid).vts <= v) lo = mid
      else hi = mid - 1
    }
    return lo
  }

  /** Index of the first frame with tick >= t, or -1. */
  indexAtTick(t: number): number {
    let lo = 0
    let hi = this._count - 1
    if (hi < 0) return -1
    if (t > this.frameAt(hi).tick) return -1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (this.frameAt(mid).tick < t) lo = mid + 1
      else hi = mid
    }
    return lo
  }

  /** Virtual time of a tick's frame; undefined when that tick has no frame (yet). */
  vtsOfTick(t: number): number | undefined {
    const i = this.indexAtTick(t)
    if (i < 0) return undefined
    const f = this.frameAt(i)
    return f.tick === t ? f.vts : undefined
  }

  /** Tick shown at the render clock (tick of the older bracketing frame). */
  renderTick(): number {
    if (this._count === 0) return 0
    return this.frameAt(this.indexAtVts(this.playback.renderVts)).tick
  }

  renderIndex(): number {
    return this._count === 0 ? -1 : this.indexAtVts(this.playback.renderVts)
  }

  delay(): number {
    return DELAY_FACTOR * this.ema
  }

  liveTarget(): number {
    const t = this.newestVts - this.delay()
    return t < this.oldestVts ? this.oldestVts : t
  }

  // ------------------------------------------------------------ render clock

  /**
   * Advance the render clock by `dtSec` of wall time. Returns the new renderVts.
   * Live: chase `newestVts − 1.25·ema` at speed·rate, rate ∈ {0.85, 1, 1.15} by lag,
   * never beyond newestVts, snapping when more than 3·ema behind.
   * Scrub: free-run at `speed`, clamped to the buffer.
   */
  advance(dtSec: number): number {
    const p = this.playback
    if (this._count === 0) return p.renderVts
    const dtMs = (dtSec > 0.1 ? 0.1 : dtSec < 0 ? 0 : dtSec) * 1000
    if (p.mode === 'live') {
      const target = this.liveTarget()
      if (p.easing) {
        p.easeT += dtMs
        const k = smoothstep(p.easeT / EASE_MS)
        p.renderVts = p.easeFrom + (target - p.easeFrom) * k
        if (p.easeT >= EASE_MS) p.easing = false
      } else {
        const err = target - p.renderVts
        const band = 0.5 * this.delay()
        if (err > CLAMP_FACTOR * this.ema) {
          p.renderVts = target
          p.rate = 1
          p.snaps++
        } else {
          p.rate = err > band ? 1.15 : err < -band ? 0.85 : 1
          p.renderVts += dtMs * p.speed * p.rate
        }
      }
    } else {
      p.rate = 1
      p.renderVts += dtMs * p.speed
    }
    if (p.renderVts > this.newestVts) p.renderVts = this.newestVts
    if (p.renderVts < this.oldestVts) p.renderVts = this.oldestVts
    return p.renderVts
  }

  /** Snap straight to the live target (visibilitychange, reconnect). */
  snap(): void {
    const p = this.playback
    if (this._count === 0) return
    if (p.mode === 'live') {
      p.renderVts = this.liveTarget()
      p.easing = false
      p.snaps++
    }
  }

  // ------------------------------------------------------------ playback controls

  setSpeed(speed: PlaybackSpeed): void {
    const p = this.playback
    if (speed !== 1 && p.mode === 'live') p.mode = 'scrub'
    if (speed !== 0) this.lastSpeed = speed
    p.speed = speed
    p.easing = false
  }

  togglePause(): void {
    const p = this.playback
    if (p.speed === 0) this.setSpeed(this.lastSpeed === 0 ? 1 : this.lastSpeed)
    else {
      if (p.mode === 'live') p.mode = 'scrub'
      p.speed = 0
      p.easing = false
    }
  }

  goLive(): void {
    const p = this.playback
    p.mode = 'live'
    p.speed = 1
    p.easing = true
    p.easeFrom = p.renderVts
    p.easeT = 0
  }

  scrubToIndex(i: number): void {
    if (this._count === 0) return
    const idx = i < 0 ? 0 : i >= this._count ? this._count - 1 : Math.floor(i)
    const p = this.playback
    p.mode = 'scrub'
    p.easing = false
    p.renderVts = this.frameAt(idx).vts
  }

  scrubToVts(v: number): void {
    const p = this.playback
    p.mode = 'scrub'
    p.easing = false
    p.renderVts = v < this.oldestVts ? this.oldestVts : v > this.newestVts ? this.newestVts : v
  }

  step(dir: 1 | -1): void {
    if (this._count === 0) return
    const p = this.playback
    const i = this.indexAtVts(p.renderVts)
    let target = i
    if (dir > 0) target = Math.min(this._count - 1, i + 1)
    else {
      const f = this.frameAt(i)
      target = p.renderVts > f.vts + 1 ? i : Math.max(0, i - 1)
    }
    p.mode = 'scrub'
    p.speed = 0
    p.easing = false
    p.renderVts = this.frameAt(target).vts
  }

  // ------------------------------------------------------------ sampling

  /** Zero-allocation interpolation of the world at `renderVts` into `out`. */
  sample(renderVts: number, out: SampleOut): void {
    const n = this._count
    if (n === 0) {
      out.count = 0
      out.nodeCount = 0
      out.frameA = out.frameB = null
      return
    }
    let v = renderVts
    if (v < this.oldestVts) v = this.oldestVts
    if (v > this.newestVts) v = this.newestVts
    const i = this.indexAtVts(v)
    const A = this.frameAt(i)
    const B = i + 1 < n ? this.frameAt(i + 1) : A
    const segLen = B.vts - A.vts
    const t = segLen > 0 ? (v - A.vts) / segLen : 0
    const invSeg = segLen > 0 ? 1000 / segLen : 0

    out.frameA = A
    out.frameB = B
    out.tickA = A.tick
    out.tickB = B.tick
    out.frac = t
    out.tick = A.tick
    out.day = A.day
    out.tickOfDay = A.tickOfDay
    out.weather = A.world.weather + (B.world.weather - A.world.weather) * t
    out.scarcity = A.world.scarcity + (B.world.scarcity - A.world.scarcity) * t

    const cap = out.capacity < this.capacity ? out.capacity : this.capacity
    out.count = cap
    for (let s = 0; s < cap; s++) {
      const inA = s < A.capacity
      const inB = s < B.capacity
      const idA = inA ? A.ids[s]! : ''
      const idB = inB ? B.ids[s]! : ''
      const stA = inA && idA !== '' ? A.flags[s * FLAG_STRIDE + 1]! : STATUS_ABSENT
      const stB = inB && idB !== '' ? B.flags[s * FLAG_STRIDE + 1]! : STATUS_ABSENT
      const dA = s * SLOT_STRIDE
      const dB = s * SLOT_STRIDE
      const fB = s * FLAG_STRIDE
      const fA = s * FLAG_STRIDE

      if (idB !== '' && stB === STATUS_ALIVE) {
        // Alive in the newer frame: interpolate when the same id is in the older frame.
        if (idA === idB) {
          out.x[s] = A.data[dA]! + (B.data[dB]! - A.data[dA]!) * t
          out.y[s] = A.data[dA + 1]! + (B.data[dB + 1]! - A.data[dA + 1]!) * t
          out.heading[s] = shortestArc(A.data[dA + 2]!, B.data[dB + 2]!, t)
          out.stress[s] = A.data[dA + 3]! + (B.data[dB + 3]! - A.data[dA + 3]!) * t
          out.tEff[s] = lerpNaN(A.data[dA + 4]!, B.data[dB + 4]!, t)
          out.balance[s] = A.data[dA + 5]! + (B.data[dB + 5]! - A.data[dA + 5]!) * t
          out.vx[s] = (B.data[dB]! - A.data[dA]!) * invSeg
          out.vy[s] = (B.data[dB + 1]! - A.data[dA + 1]!) * invSeg
          out.asleep[s] = A.flags[fA]! & B.flags[fB]!
          out.degenerate[s] = A.flags[fA + 2]! | B.flags[fB + 2]!
        } else {
          out.x[s] = B.data[dB]!
          out.y[s] = B.data[dB + 1]!
          out.heading[s] = B.data[dB + 2]!
          out.stress[s] = B.data[dB + 3]!
          out.tEff[s] = B.data[dB + 4]!
          out.balance[s] = B.data[dB + 5]!
          out.vx[s] = 0
          out.vy[s] = 0
          out.asleep[s] = B.flags[fB]!
          out.degenerate[s] = B.flags[fB + 2]!
        }
        out.ids[s] = idB
        out.status[s] = STATUS_ALIVE
        out.anim[s] = B.flags[fB + 3]!
        out.dead[s] = 0
        out.alpha[s] = 1
        const born = this.slotBornVts[s]!
        // Scale in over the 400 ms leading up to the first frame that holds the id.
        const k = (v - (born - BIRTH_MS)) / BIRTH_MS
        out.scale[s] = born === Number.POSITIVE_INFINITY ? 1 : k < 0 ? 0 : k > 1 ? 1 : k
        out.present[s] = 1
        continue
      }

      // Missing or non-alive in the newer frame: tombstone.
      const died = this.slotDiedVts[s]!
      const age = v - died
      if (died !== Number.POSITIVE_INFINITY && age < TOMBSTONE_MS && age > -1e9) {
        if (idA !== '' && idA === idB) {
          out.x[s] = A.data[dA]! + (B.data[dB]! - A.data[dA]!) * t
          out.y[s] = A.data[dA + 1]! + (B.data[dB + 1]! - A.data[dA + 1]!) * t
          out.heading[s] = shortestArc(A.data[dA + 2]!, B.data[dB + 2]!, t)
          out.stress[s] = A.data[dA + 3]! + (B.data[dB + 3]! - A.data[dA + 3]!) * t
          out.tEff[s] = lerpNaN(A.data[dA + 4]!, B.data[dB + 4]!, t)
          out.balance[s] = A.data[dA + 5]! + (B.data[dB + 5]! - A.data[dA + 5]!) * t
          out.ids[s] = idA
          out.asleep[s] = A.flags[fA]!
          out.degenerate[s] = A.flags[fA + 2]!
          out.anim[s] = A.flags[fA + 3]!
        } else if (idA !== '' && stA === STATUS_ALIVE) {
          out.x[s] = A.data[dA]!
          out.y[s] = A.data[dA + 1]!
          out.heading[s] = A.data[dA + 2]!
          out.stress[s] = A.data[dA + 3]!
          out.tEff[s] = A.data[dA + 4]!
          out.balance[s] = A.data[dA + 5]!
          out.ids[s] = idA
          out.asleep[s] = A.flags[fA]!
          out.degenerate[s] = A.flags[fA + 2]!
          out.anim[s] = A.flags[fA + 3]!
        } else {
          const L = this.slotLast
          out.x[s] = L[dA]!
          out.y[s] = L[dA + 1]!
          out.heading[s] = L[dA + 2]!
          out.stress[s] = L[dA + 3]!
          out.tEff[s] = L[dA + 4]!
          out.balance[s] = L[dA + 5]!
          out.ids[s] = this.slotDeadId[s]!
          out.asleep[s] = 0
          out.degenerate[s] = 0
          out.anim[s] = ANIM_DEAD
        }
        out.vx[s] = 0
        out.vy[s] = 0
        const isDead = age >= 0
        out.dead[s] = isDead ? 1 : 0
        out.status[s] = isDead ? (stB === STATUS_ABSENT ? STATUS_ARCHIVED : stB) : STATUS_ALIVE
        out.alpha[s] = isDead ? 1 - age / TOMBSTONE_MS : 1
        out.scale[s] = 1
        out.present[s] = 1
        continue
      }

      out.present[s] = 0
      out.ids[s] = ''
      out.alpha[s] = 0
      out.scale[s] = 0
      out.dead[s] = 0
      out.status[s] = STATUS_ABSENT
    }

    const ncap = out.nodeCapacity < this.nodeCapacity ? out.nodeCapacity : this.nodeCapacity
    out.nodeCount = ncap
    for (let s = 0; s < ncap; s++) {
      const idB = s < B.nodeCapacity ? B.nodeIds[s]! : ''
      const idA = s < A.nodeCapacity ? A.nodeIds[s]! : ''
      const d = s * NODE_STRIDE
      if (idB === '' && idA === '') {
        out.nodePresent[s] = 0
        out.nodeIds[s] = ''
        continue
      }
      if (idA === idB) {
        out.nx[s] = A.nodes[d]! + (B.nodes[d]! - A.nodes[d]!) * t
        out.ny[s] = A.nodes[d + 1]! + (B.nodes[d + 1]! - A.nodes[d + 1]!) * t
        out.nstock[s] = A.nodes[d + 2]! + (B.nodes[d + 2]! - A.nodes[d + 2]!) * t
      } else {
        const F = idB !== '' ? B : A
        out.nx[s] = F.nodes[d]!
        out.ny[s] = F.nodes[d + 1]!
        out.nstock[s] = F.nodes[d + 2]!
      }
      const F = idB !== '' ? B : A
      out.ncap[s] = F.nodes[d + 3]!
      out.ndelta[s] = F.nodes[d + 4]!
      out.nodeIds[s] = idB !== '' ? idB : idA
      out.nodePresent[s] = 1
    }
  }
}

/** The module singleton (§16). Tests construct their own instances. */
export const history = new History(600)
