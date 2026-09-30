/**
 * Module-level state shared by the scene systems. Nothing here is React state:
 * the frame loop reads and writes it directly. Store-derived lookups (tier
 * colours, hover/selection) are mirrored in by subscriptions outside useFrame.
 */
import { Color } from 'three'
import type { HelloConfig, RosterAgent } from '../protocol'
import { history, SampleOut } from '../state/history'
import { selectTierColor, useStore } from '../state/store'

export interface FrameCtx {
  /** Wall-clock delta (s), clamped. */
  dt: number
  /** Wall-clock seconds since page start. */
  now: number
  renderVts: number
  /** Playback speed × lag-control rate; 0 when paused. */
  speed: number
  out: SampleOut
  reducedMotion: boolean
  /** World units per pixel at unit distance: 2·tan(fov/2) / viewport height (in device pixels). */
  pixelAngle: number
}

type System = (ctx: FrameCtx) => void

const systems: Array<{ order: number; fn: System }> = []

export function registerSystem(order: number, fn: System): () => void {
  const entry = { order, fn }
  systems.push(entry)
  systems.sort((a, b) => a.order - b.order)
  return () => {
    const i = systems.indexOf(entry)
    if (i >= 0) systems.splice(i, 1)
  }
}

export function runSystems(ctx: FrameCtx): void {
  for (let i = 0; i < systems.length; i++) systems[i]!.fn(ctx)
}

export const SYS_AGENTS = 10
export const SYS_NODES = 20
export const SYS_EFFECTS = 30
export const SYS_ATMOSPHERE = 40
export const SYS_CAMERA = 50

/** The sampled world for the current frame. Rebuilt when history capacity grows. */
export const sampled = {
  out: new SampleOut(history.capacity, history.nodeCapacity),
}

history.onCapacityChange((cap, ncap) => {
  sampled.out = new SampleOut(cap, ncap)
  const p = perSlot
  if (cap > p.capacity) growPerSlot(cap)
})

/** Per-slot scratch that must survive across frames (anim weights, fx timers). */
export const perSlot = {
  capacity: 0,
  /** idle, walk, sleep, degenerate, dead */
  weights: new Float32Array(0),
  phase: new Float32Array(0),
  degenUntil: new Float64Array(0),
  flashUntil: new Float64Array(0),
  /** cached id → colour resolution */
  colorId: [] as string[],
  colorR: new Float32Array(0),
  colorG: new Float32Array(0),
  colorB: new Float32Array(0),
}

function growPerSlot(cap: number): void {
  const p = perSlot
  const weights = new Float32Array(cap * 5)
  weights.set(p.weights)
  const phase = new Float32Array(cap)
  phase.set(p.phase)
  for (let i = p.capacity; i < cap; i++) phase[i] = (i * 0.61803398875) % 1
  const degenUntil = new Float64Array(cap)
  degenUntil.set(p.degenUntil)
  const flashUntil = new Float64Array(cap)
  flashUntil.set(p.flashUntil)
  const colorId = new Array<string>(cap).fill('')
  for (let i = 0; i < p.capacity; i++) colorId[i] = p.colorId[i]!
  const colorR = new Float32Array(cap)
  colorR.set(p.colorR)
  const colorG = new Float32Array(cap)
  colorG.set(p.colorG)
  const colorB = new Float32Array(cap)
  colorB.set(p.colorB)
  p.capacity = cap
  p.weights = weights
  p.phase = phase
  p.degenUntil = degenUntil
  p.flashUntil = flashUntil
  p.colorId = colorId
  p.colorR = colorR
  p.colorG = colorG
  p.colorB = colorB
}
growPerSlot(history.capacity)

// ------------------------------------------------------------ store mirrors

export const mirror = {
  rosterById: new Map<string, RosterAgent>() as ReadonlyMap<string, RosterAgent>,
  config: null as HelloConfig | null,
  tierColor: new Map<string, Color>(),
  selectedId: null as string | null,
  hoveredId: null as string | null,
  hoveredGadgetId: null as string | null,
  followId: null as string | null,
  reducedMotion: false,
  worldSize: 60,
}

const scratch = new Color()

function rebuildTierColors(): void {
  mirror.tierColor.clear()
  const cfg = mirror.config
  if (!cfg) return
  for (const tier of Object.keys(cfg.tiers)) {
    mirror.tierColor.set(tier, new Color(selectTierColor(cfg, tier)))
  }
  perSlot.colorId.fill('')
}

useStore.subscribe((s, prev) => {
  if (s.rosterById !== prev.rosterById) {
    mirror.rosterById = s.rosterById
    perSlot.colorId.fill('')
  }
  if (s.config !== prev.config) {
    mirror.config = s.config
    mirror.worldSize = s.config?.world_size ?? 60
    rebuildTierColors()
  }
  mirror.selectedId = s.selectedAgentId
  mirror.hoveredId = s.hoveredAgentId
  mirror.hoveredGadgetId = s.hoveredGadgetId
  mirror.followId = s.followAgentId
  mirror.reducedMotion = s.reducedMotion
})
{
  const s = useStore.getState()
  mirror.rosterById = s.rosterById
  mirror.config = s.config
  mirror.worldSize = s.config?.world_size ?? 60
  mirror.reducedMotion = s.reducedMotion
  rebuildTierColors()
}

const FALLBACK = new Color('#8b93a7')

/** Resolve (and cache per slot) the tier colour of the agent currently in a slot. */
export function slotColor(slot: number, id: string): Color {
  const p = perSlot
  if (p.colorId[slot] !== id) {
    const tier = mirror.rosterById.get(id)?.tier
    const c = (tier && mirror.tierColor.get(tier)) || FALLBACK
    p.colorId[slot] = id
    p.colorR[slot] = c.r
    p.colorG[slot] = c.g
    p.colorB[slot] = c.b
  }
  scratch.setRGB(p.colorR[slot]!, p.colorG[slot]!, p.colorB[slot]!)
  return scratch
}

/** Commands the keyboard layer sends to the camera rig. */
export const cameraCommands = {
  frameRequested: false,
}

/** World (x, y) → scene (x, z). Height is scene y. */
export const WORLD_TO_SCENE_Z = 1
