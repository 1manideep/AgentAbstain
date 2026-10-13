/**
 * Behaviour controller for the procedural characters, written as pure functions over the sampled world (no
 * three.js, no React, no scene state) so CharacterSystem.tsx stays a thin driver and everything here is
 * unit-tested.
 *
 * Gestures. `gestureFor(event)` maps a world event to the gesture(s) it should show: who plays which clip, for
 * how long, whom they face and how urgent it is. Each puppet keeps a GestureState (the current gesture and a
 * queue of at most QUEUE_DEPTH); `applyGesture` is its reducer: a gesture with a priority >= the current one's
 * replaces it, a lower one waits in the queue (the oldest waiting gesture is dropped when the queue is full) and
 * a repeat of the current clip extends a hold or lets a one-shot finish rather than restarting it (scripted
 * brains talk every tick). `hold` is the time a gesture occupies the agent: a looping clip (write, read,
 * tinker, think) loops for `hold` seconds and fades out, a one-shot (talk, give, receive, summon, shake, cheer,
 * slump, wave) plays once, so its hold is its clip duration. Walking cancels the standing holds
 * (`cancelStandingHolds`); every other gesture plays on top of the walk at WALK_GESTURE_WEIGHT. Forage is
 * not a gesture: the forage loop in CharacterSystem is driven by the forage event and the last action.
 *
 * Facing and glances. Headings follow the snapshot convention (radians, 0 = +x, world y is scene z); the
 * renderer turns a heading into the root yaw `Math.PI / 2 - heading`. `targetYawTo` is the heading from one
 * point to another, `facingYaw` approaches a heading along the shortest arc at an exponential rate (the same
 * call decays back to the snapshot heading once the facing timer runs out) and `headYawFor` is the local yaw
 * the head bone needs, clamped to +-HEAD_YAW_MAX, to look at a point given the body's heading. Facing applies
 * to a standing agent (speed < STAND_VEL) whose target is within FACE_RANGE; a glance to an idle agent within
 * GLANCE_RANGE of a speaker.
 *
 * Style. The six roster traits (TRAIT_DEFAULT when absent) and the agent's stress become a Style:
 *   fidget        = clamp01(0.55·curiosity + 0.45·(1 − caution) + 0.5·stress − 0.35)
 *   guard         = clamp01(0.6·caution + 0.5·greed − 0.3)
 *   brisk         = clamp01(1.4·industriousness − 0.45)
 *   idleTimeScale = 0.85 + 0.35·curiosity
 *   walkTimeScale = 0.9 + 0.25·industriousness
 *   leanForward   = 0.05·(industriousness − caution)   radians about the spine; off under reduced motion
 * `splitBase` turns the sampled idle weight into idle / idle_fidget / idle_guard (fidget and guard normalised
 * so the three sum to the idle weight) and the walk weight into walk / walk_brisk by brisk. Both walk loops
 * run from one stride time scale (`walkTimeScale`); the brisk loop is shorter, so the driver scales its time
 * by BRISK_RATIO and phase-locks it to the walk so the legs stay in step.
 *
 * Events reach the controller through a GestureQueue keyed by tick, like the effects' FxQueue, but accepting
 * the kinds in BEHAVIOUR_KINDS (FxQueue filters on its own FX_KINDS).
 */
import type { EventMsg } from '../../protocol'
import { CLIP_DURATION, CLIP_LOOPS, type ClipName } from './clipNames'

const TAU = Math.PI * 2

// ------------------------------------------------------------ gestures

export interface Gesture {
  agentId: string
  /** null: no body gesture, only a facing target (the listener of a talk turns to the speaker). */
  clip: ClipName | null
  /** Seconds the gesture occupies the agent: a one-shot's clip duration, a loop's hold. */
  hold: number
  /** Agent to face while standing within FACE_RANGE, for `faceFor` seconds (FACE_DEFAULT when absent). */
  faceId?: string
  faceFor?: number
  priority: number
  /** Always wait behind whatever is current (the cheer after a windfall's receive). */
  queued?: boolean
}

export interface ActiveGesture {
  clip: ClipName
  hold: number
  priority: number
  /** Wall-clock second the gesture started (ctx.now). */
  start: number
}

export interface GestureState {
  current: ActiveGesture | null
  queue: readonly ActiveGesture[]
}

const EMPTY_QUEUE: readonly ActiveGesture[] = []
/** The idle state; shared, never mutated. */
export const NO_GESTURE: GestureState = { current: null, queue: EMPTY_QUEUE }

export const QUEUE_DEPTH = 2
/** Shake and slump interrupt everything. */
export const PRIORITY_URGENT = 3
/** Give, receive and talk: exchanges between two agents. */
export const PRIORITY_EXCHANGE = 2
/** Holds and the remaining one-shots. */
export const PRIORITY_HOLD = 1

/** Looping gestures that only make sense standing still; walking cancels them. */
export const STANDING_HOLDS: ReadonlySet<ClipName> = new Set<ClipName>(['write', 'read', 'tinker', 'think'])

/** How long a talk's speaker faces the listener, the listener faces the speaker, and any other pairing. */
export const FACE_TALK = 2.5
export const FACE_LISTEN = 2
export const FACE_DEFAULT = 2
export const HOLD_READ = 3
export const HOLD_TINKER = 2.5
export const HOLD_WRITE = 2
export const HOLD_MAINTENANCE = 4

/** Event kinds `gestureFor` turns into gestures (sleep and death are left to the base states). */
export const GESTURE_KINDS: ReadonlySet<string> = new Set([
  'talk',
  'transfer',
  'share_note',
  'windfall',
  'read_chronicle',
  'nudge_weather',
  'propose_gadget',
  'use_gadget',
  'gadget_verified',
  'gadget_rejected',
  'remember',
  'revise_self',
  'maintenance',
  'claim_feedback',
  'gate_blocked',
  'action_failed',
  'birth',
  'task_assigned',
  'task_completed',
])

/** Everything the character controller polls from the event ring: the gesture kinds plus forage. */
export const BEHAVIOUR_KINDS: ReadonlySet<string> = new Set([...GESTURE_KINDS, 'forage'])

export function priorityOf(clip: ClipName): number {
  switch (clip) {
    case 'shake':
    case 'slump':
      return PRIORITY_URGENT
    case 'give':
    case 'receive':
    case 'talk':
      return PRIORITY_EXCHANGE
    default:
      return PRIORITY_HOLD
  }
}

const NO_GESTURES: readonly Gesture[] = []

function str(p: Record<string, unknown>, key: string): string | null {
  const v = p[key]
  return typeof v === 'string' && v !== '' ? v : null
}

/** The acting agent: the payload's agent_id, else the event's. */
function actor(e: EventMsg, p: Record<string, unknown>): string | null {
  return str(p, 'agent_id') ?? e.agent_id
}

/** A one-shot: plays once, so it holds for its clip duration. */
function shot(agentId: string, clip: ClipName, faceId?: string, faceFor?: number): Gesture {
  const g: Gesture = { agentId, clip, hold: CLIP_DURATION[clip], priority: priorityOf(clip) }
  if (faceId !== undefined) {
    g.faceId = faceId
    g.faceFor = faceFor ?? FACE_DEFAULT
  }
  return g
}

/** A looping clip held for `seconds`. */
function held(agentId: string, clip: ClipName, seconds: number): Gesture {
  return { agentId, clip, hold: seconds, priority: priorityOf(clip) }
}

/** Speaker gives, listener receives, each facing the other (transfer, share_note). */
function exchange(from: string | null, to: string | null): readonly Gesture[] {
  const out: Gesture[] = []
  if (from) out.push(shot(from, 'give', to ?? undefined))
  if (to) out.push(shot(to, 'receive', from ?? undefined))
  return out
}

function single(agentId: string | null, make: (id: string) => Gesture): readonly Gesture[] {
  return agentId ? [make(agentId)] : NO_GESTURES
}

/** The gesture(s) a world event shows; [] for kinds the base states or nothing at all should carry. */
export function gestureFor(e: EventMsg): readonly Gesture[] {
  const p = e.payload as Record<string, unknown>
  switch (e.kind) {
    case 'talk': {
      const speaker = str(p, 'speaker_id')
      const listener = str(p, 'listener_id')
      const out: Gesture[] = []
      if (speaker) out.push(shot(speaker, 'talk', listener ?? undefined, FACE_TALK))
      if (listener && speaker) out.push({ agentId: listener, clip: null, hold: 0, faceId: speaker, faceFor: FACE_LISTEN, priority: 0 })
      return out
    }
    case 'transfer':
      return exchange(str(p, 'from'), str(p, 'to'))
    case 'share_note':
      return exchange(str(p, 'speaker_id'), str(p, 'listener_id'))
    case 'windfall': {
      const id = actor(e, p)
      if (!id) return NO_GESTURES
      const cheer = shot(id, 'cheer')
      cheer.queued = true
      return [shot(id, 'receive'), cheer]
    }
    case 'read_chronicle':
      return single(actor(e, p), (id) => held(id, 'read', HOLD_READ))
    case 'nudge_weather':
      return single(actor(e, p), (id) => shot(id, 'summon'))
    case 'propose_gadget':
    case 'use_gadget':
      return single(actor(e, p), (id) => held(id, 'tinker', HOLD_TINKER))
    case 'gadget_verified':
      return single(str(p, 'owner') ?? e.agent_id, (id) => held(id, 'tinker', HOLD_TINKER))
    case 'gadget_rejected':
      return single(str(p, 'owner') ?? e.agent_id, (id) => shot(id, 'slump'))
    case 'remember':
    case 'revise_self':
      return single(actor(e, p), (id) => held(id, 'write', HOLD_WRITE))
    case 'maintenance':
      return single(actor(e, p), (id) => held(id, 'write', HOLD_MAINTENANCE))
    case 'claim_feedback': {
      const listener = str(p, 'listener_id') ?? e.agent_id
      const speaker = str(p, 'speaker_id') ?? undefined
      return single(listener, (id) => shot(id, 'shake', speaker))
    }
    case 'gate_blocked':
      return single(actor(e, p), (id) => shot(id, 'slump'))
    case 'action_failed':
      // only a rejected action is worth a slump; a stale one (the world moved on) is silent
      return str(p, 'kind') === 'invalid' ? single(actor(e, p), (id) => shot(id, 'slump')) : NO_GESTURES
    case 'birth':
      return single(actor(e, p), (id) => shot(id, 'wave'))
    case 'task_assigned':
    case 'task_completed':
      return single(actor(e, p), (id) => shot(id, 'cheer'))
    default:
      return NO_GESTURES
  }
}

/**
 * Reducer: fold a gesture into a puppet's state at wall-clock `now`. Returns the same object when nothing
 * changes (face-only gestures, a one-shot repeating itself), so the driver can compare by identity.
 */
export function applyGesture(state: GestureState, g: Gesture, now: number): GestureState {
  if (g.clip === null) return state
  const entry: ActiveGesture = { clip: g.clip, hold: g.hold, priority: g.priority, start: now }
  const cur = state.current
  if (!cur) return { current: entry, queue: state.queue }
  if (!g.queued) {
    if (cur.clip === g.clip && g.priority >= cur.priority) {
      // a repeat: a loop keeps going for at least another hold, a one-shot finishes what it started
      if (!CLIP_LOOPS[cur.clip]) return state
      const until = Math.max(cur.start + cur.hold, now + g.hold)
      return { current: { clip: cur.clip, hold: until - cur.start, priority: cur.priority, start: cur.start }, queue: state.queue }
    }
    if (g.priority >= cur.priority) return { current: entry, queue: state.queue }
  }
  const q = state.queue
  const queue: ActiveGesture[] = []
  for (let i = q.length >= QUEUE_DEPTH ? q.length - QUEUE_DEPTH + 1 : 0; i < q.length; i++) queue.push(q[i]!)
  queue.push(entry)
  return { current: cur, queue }
}

/** The current gesture is over: start the next queued one at `now`, or go idle. */
export function finishCurrent(state: GestureState, now: number): GestureState {
  const q = state.queue
  if (q.length === 0) return state.current === null ? state : NO_GESTURE
  const next = q[0]!
  const rest: ActiveGesture[] = []
  for (let i = 1; i < q.length; i++) rest.push(q[i]!)
  return { current: { clip: next.clip, hold: next.hold, priority: next.priority, start: now }, queue: rest.length === 0 ? EMPTY_QUEUE : rest }
}

/** The agent walks: drop the standing holds (current and queued). Same object back when there are none. */
export function cancelStandingHolds(state: GestureState, now: number): GestureState {
  const cur = state.current
  const q = state.queue
  let dropQueued = false
  for (let i = 0; i < q.length; i++) if (STANDING_HOLDS.has(q[i]!.clip)) dropQueued = true
  const dropCur = cur !== null && STANDING_HOLDS.has(cur.clip)
  if (!dropCur && !dropQueued) return state
  const kept: ActiveGesture[] = []
  for (let i = 0; i < q.length; i++) if (!STANDING_HOLDS.has(q[i]!.clip)) kept.push(q[i]!)
  if (!dropCur) return { current: cur, queue: kept.length === 0 ? EMPTY_QUEUE : kept }
  if (kept.length === 0) return NO_GESTURE
  const next = kept[0]!
  const rest: ActiveGesture[] = []
  for (let i = 1; i < kept.length; i++) rest.push(kept[i]!)
  return { current: { clip: next.clip, hold: next.hold, priority: next.priority, start: now }, queue: rest.length === 0 ? EMPTY_QUEUE : rest }
}

// ------------------------------------------------------------ envelopes and weights

/** One-shots ramp in and out from their own action time (the talk envelope the system always had). */
export const SHOT_ATTACK = 0.15
export const SHOT_RELEASE = 0.25
/** Holds ramp from wall-clock time since they started. */
export const HOLD_ATTACK = 0.25
export const HOLD_RELEASE = 0.35
/** Weight of a gesture played on top of the walk. */
export const WALK_GESTURE_WEIGHT = 0.6
/** Gesture weight cap under reduced motion. */
export const REDUCED_GESTURE_CAP = 0.7

/** Trapezoid 0→1→0 over [0, total] with linear attack and release; 0 outside. */
export function envelope(t: number, total: number, attack: number, release: number): number {
  if (t <= 0 || t >= total) return 0
  return Math.min(1, t / attack, (total - t) / release)
}

/** Factor on a gesture's weight for the agent's walk weight (0..1) and the reduced-motion preference. */
export function gestureScale(ww: number, reducedMotion: boolean): number {
  const w = ww < 0 ? 0 : ww > 1 ? 1 : ww
  const s = 1 - (1 - WALK_GESTURE_WEIGHT) * w
  return reducedMotion && s > REDUCED_GESTURE_CAP ? REDUCED_GESTURE_CAP : s
}

// ------------------------------------------------------------ facing and glances

/** Below this speed (world units / s) an agent counts as standing. */
export const STAND_VEL = 0.05
/** Facing targets further away are ignored. */
export const FACE_RANGE = 8
/** Idle agents glance at speakers this close. */
export const GLANCE_RANGE = 6
/** Head yaw clamp (rad). */
export const HEAD_YAW_MAX = 0.9
/** Exponential rates (1/s): turning to face, returning to the snapshot heading, the head glance. */
export const FACE_RATE = 6
export const RETURN_RATE = 10
export const HEAD_RATE = 8

/** Wrap an angle into (-π, π]. */
export function wrapAngle(a: number): number {
  let d = a % TAU
  if (d > Math.PI) d -= TAU
  else if (d <= -Math.PI) d += TAU
  return d
}

/** Exponential approach of `current` to `target` along the shortest arc; the result is wrapped. */
export function facingYaw(current: number, target: number, dt: number, rate: number): number {
  const d = wrapAngle(target - current)
  return wrapAngle(current + d * (1 - Math.exp(-dt * rate)))
}

/** Heading (snapshot convention: 0 = +x, world y) from (x, z) towards (tx, tz). */
export function targetYawTo(x: number, z: number, tx: number, tz: number): number {
  return Math.atan2(tz - z, tx - x)
}

/**
 * Local yaw for the head bone of a body with `heading` at (x, z) to look at (tx, tz), clamped to
 * +-HEAD_YAW_MAX. Positive turns the head to the body's left (towards smaller headings), since the root is
 * rotated by π/2 − heading.
 */
export function headYawFor(x: number, z: number, heading: number, tx: number, tz: number): number {
  const d = wrapAngle(heading - targetYawTo(x, z, tx, tz))
  return d > HEAD_YAW_MAX ? HEAD_YAW_MAX : d < -HEAD_YAW_MAX ? -HEAD_YAW_MAX : d
}

// ------------------------------------------------------------ personality style

export interface Style {
  fidget: number
  guard: number
  brisk: number
  idleTimeScale: number
  walkTimeScale: number
  /** Radians of extra forward spine tilt (negative leans back). */
  leanForward: number
}

export interface BaseSplit {
  idle: number
  fidget: number
  guard: number
  walk: number
  brisk: number
}

export const TRAIT_DEFAULT = 0.5
/** World units covered by one walk cycle at time scale 1. */
export const STRIDE = 1.3
/** The brisk loop is shorter than the walk loop; its time runs at this ratio so both cycle together. */
export const BRISK_RATIO = CLIP_DURATION.walk_brisk / CLIP_DURATION.walk

export function makeStyle(): Style {
  return { fidget: 0, guard: 0, brisk: 0, idleTimeScale: 1, walkTimeScale: 1, leanForward: 0 }
}

export function makeSplit(): BaseSplit {
  return { idle: 0, fidget: 0, guard: 0, walk: 0, brisk: 0 }
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v
}

function trait(traits: Readonly<Record<string, number>> | undefined, key: string): number {
  const v = traits === undefined ? undefined : traits[key]
  return typeof v === 'number' && Number.isFinite(v) ? clamp01(v) : TRAIT_DEFAULT
}

/** Style from the roster traits (TRAIT_DEFAULT each when absent) and stress; writes into `out` when given. */
export function styleFor(traits: Readonly<Record<string, number>> | undefined, stress: number, out: Style = makeStyle()): Style {
  const greed = trait(traits, 'greed')
  const curiosity = trait(traits, 'curiosity')
  const caution = trait(traits, 'caution')
  const industriousness = trait(traits, 'industriousness')
  const s = clamp01(Number.isFinite(stress) ? stress : 0)
  out.fidget = clamp01(0.55 * curiosity + 0.45 * (1 - caution) + 0.5 * s - 0.35)
  out.guard = clamp01(0.6 * caution + 0.5 * greed - 0.3)
  out.brisk = clamp01(1.4 * industriousness - 0.45)
  out.idleTimeScale = 0.85 + 0.35 * curiosity
  out.walkTimeScale = 0.9 + 0.25 * industriousness
  out.leanForward = 0.05 * (industriousness - caution)
  return out
}

/**
 * Split the idle weight into idle / idle_fidget / idle_guard (summing to `wi`; fidget and guard are scaled
 * down together when they would exceed it) and the walk weight into walk / walk_brisk (summing to `ww`).
 */
export function splitBase(wi: number, ww: number, style: Style, out: BaseSplit = makeSplit()): BaseSplit {
  let f = clamp01(style.fidget)
  let g = clamp01(style.guard)
  const sum = f + g
  if (sum > 1) {
    f /= sum
    g /= sum
  }
  const rest = 1 - f - g
  out.idle = wi * (rest < 0 ? 0 : rest)
  out.fidget = wi * f
  out.guard = wi * g
  const b = clamp01(style.brisk)
  out.walk = ww * (1 - b)
  out.brisk = ww * b
  return out
}

/** Time scale of the walk loops for a ground speed `vel` (units / s): feet keep up, within sane bounds. */
export function walkTimeScale(vel: number, style: Style): number {
  const k = vel / STRIDE
  return (k < 0.55 ? 0.55 : k > 1.9 ? 1.9 : k) * style.walkTimeScale
}

// ------------------------------------------------------------ event queue

const SEEN_LIMIT = 5000

function later(a: EventMsg, b: EventMsg): boolean {
  return a.tick !== b.tick ? a.tick > b.tick : a.seq > b.seq
}

/**
 * Events of the accepted kinds, ordered by (tick, seq), deduplicated by seq, fired when the render clock
 * reaches their tick (never on arrival) so replays and scrubs line up with the interpolated motion. Same
 * contract as the effects' FxQueue, with its own kind filter.
 */
export class GestureQueue {
  private readonly kinds: ReadonlySet<string>
  private items: EventMsg[] = []
  private seen = new Set<number>()

  constructor(kinds: ReadonlySet<string> = BEHAVIOUR_KINDS) {
    this.kinds = kinds
  }

  push(e: EventMsg): void {
    if (!this.kinds.has(e.kind)) return
    if (this.seen.has(e.seq)) return
    this.seen.add(e.seq)
    if (this.seen.size > SEEN_LIMIT) {
      // keep the set bounded: drop the oldest half by seq order
      const arr = Array.from(this.seen).sort((a, b) => a - b)
      this.seen = new Set(arr.slice(arr.length >> 1))
    }
    const a = this.items
    let i = a.length
    while (i > 0 && later(a[i - 1]!, e)) i--
    if (i === a.length) a.push(e)
    else a.splice(i, 0, e)
  }

  /**
   * Fire every event whose tick <= `renderTick`; those older than `renderTick - staleTicks` are dropped
   * instead (initial catch-up, fast scrubs) so a burst of history never explodes on screen.
   */
  drain(renderTick: number, fire: (e: EventMsg) => void, staleTicks = 2): void {
    const a = this.items
    while (a.length > 0 && a[0]!.tick <= renderTick) {
      const e = a.shift()!
      if (e.tick >= renderTick - staleTicks) fire(e)
    }
  }

  /** Re-queue the events with tick > fromTick (after a scrub backwards). */
  reseed(events: Iterable<EventMsg>, fromTick: number): void {
    this.items.length = 0
    this.seen.clear()
    for (const e of events) if (e.tick > fromTick) this.push(e)
  }

  clear(): void {
    this.items.length = 0
    this.seen.clear()
  }

  get size(): number {
    return this.items.length
  }
}
