/**
 * Draws visible agents as procedural humanoids (see humanoid.ts / clips.ts)
 * in place of the instanced capsule bodies. Runs as a scene system at SYS_RIG:
 * up to `rigState.maxRigged` present slots get a puppet (a humanoid for the
 * agent's preset, an AnimationMixer and one action per clip).
 *
 * Base states. The per-slot weights the capsule path computes (perSlot.weights:
 * idle, walk, sleep, degenerate, dead) drive the base clips; the agent's
 * personality (roster traits) and stress split idle into idle / idle_fidget /
 * idle_guard and walk into walk / walk_brisk (behaviour.ts `styleFor`,
 * `splitBase`), and set the idle and stride time scales.
 *
 * Behaviour. World events become gestures through behaviour.ts `gestureFor`
 * (talk, give/receive, write while remembering or maintaining, read the
 * chronicle, tinker with gadgets, summon the weather, shake at a lie found out,
 * slump at a failure, cheer at a windfall, wave at birth), folded into each
 * puppet's GestureState by priority. One-shots play once with an attack and
 * release envelope from their own action time; holds loop for their hold and
 * fade. Walking cancels standing holds and plays the rest on top at reduced
 * weight. The forage loop still runs while the agent forages standing still.
 * A gesture may name someone to face: a standing agent turns to them along the
 * shortest arc and turns back afterwards; idle agents near a speaker glance at
 * them with the head; industrious agents lean forward a little.
 *
 * Slots drawn here are marked in `rigState.riggedSlots` so Agents.tsx skips
 * their capsule (the shadow blob and halo are still drawn there). A loaded GLB
 * rig (Rig.tsx) takes precedence: while it is active the puppets stay hidden.
 * Nothing here allocates per frame in steady state.
 */
import type { ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import { AnimationMixer, Color, Group, LoopOnce, Quaternion, Vector3, type AnimationAction, type AnimationClip, type Object3D } from 'three'
import { isEventKind, type EventMsg } from '../../protocol'
import type { EventRing } from '../../state/events'
import { history } from '../../state/history'
import { useStore } from '../../state/store'
import { gpuProfile } from '../gpu'
import { rigState } from '../rigState'
import { mirror, perSlot, registerSystem, sampled, slotColor, SYS_RIG, type FrameCtx } from '../sceneState'
import {
  applyGesture,
  BEHAVIOUR_KINDS,
  BRISK_RATIO,
  cancelStandingHolds,
  envelope,
  FACE_DEFAULT,
  FACE_RANGE,
  FACE_RATE,
  facingYaw,
  finishCurrent,
  GestureQueue,
  gestureFor,
  gestureScale,
  GLANCE_RANGE,
  HEAD_RATE,
  headYawFor,
  HOLD_ATTACK,
  HOLD_RELEASE,
  makeSplit,
  makeStyle,
  NO_GESTURE,
  RETURN_RATE,
  SHOT_ATTACK,
  SHOT_RELEASE,
  splitBase,
  STAND_VEL,
  styleFor,
  targetYawTo,
  walkTimeScale,
  type BaseSplit,
  type GestureState,
  type Style,
} from './behaviour'
import { GESTURE_CLIPS } from './clipNames'
import { buildClips, CLIP_DURATION, CLIP_LOOPS, CLIP_NAMES, type ClipName } from './clips'
import { buildHumanoid, type Humanoid } from './humanoid'
import { PRESETS, presetIndexFor } from './presets'

const W_IDLE = 0
const W_WALK = 1
const W_SLEEP = 2
const W_DEGEN = 3
const W_DEAD = 4
/** How long a forage event keeps the forage loop going (s). */
const FORAGE_HOLD = 3.2
/** How long a talk keeps the speaker interesting to nearby idle agents (s). */
const SPEAK_GLANCE = 2.2
/** A one-shot that never advanced (a hidden puppet) is released after its clip duration plus this (s). */
const SHOT_GRACE = 1.5
/** Gestures other than forage, whose weights the controller sets every frame. */
const GESTURES: readonly ClipName[] = GESTURE_CLIPS.filter((c) => c !== 'forage')
const Y_AXIS = new Vector3(0, 1, 0)
const X_AXIS = new Vector3(1, 0, 0)
const qScratch = new Quaternion()

/**
 * Characters are on unless `?rig=0`, or the renderer is a software rasteriser
 * (SwiftShader, llvmpipe) and `?rig=1` was not given to force them.
 */
export function charactersEnabled(): boolean {
  if (typeof location === 'undefined') return false
  let rig: string | null = null
  try {
    rig = new URLSearchParams(location.search).get('rig')
  } catch {
    rig = null
  }
  if (rig === '0') return false
  if (rig === '1') return true
  return !gpuProfile.software
}

interface Puppet {
  h: Humanoid
  preset: number
  mixer: AnimationMixer
  actions: Record<ClipName, AnimationAction>
  slot: number
  id: string
  name: string
  forageW: number
  forageUntil: number
  deadArmed: boolean
  gesture: GestureState
  /** Agent to face, and until when (ctx.now seconds). */
  faceId: string | null
  faceUntil: number
  /** Rendered heading (snapshot convention); NaN until the first frame. */
  yaw: number
  /** Smoothed head yaw offset for glances (rad). */
  headYaw: number
  style: Style
  split: BaseSplit
}

const clipCache = new Map<number, Record<ClipName, AnimationClip>>()

function clipsFor(preset: number): Record<ClipName, AnimationClip> {
  let c = clipCache.get(preset)
  if (!c) {
    const p = PRESETS[preset]!
    // clips depend only on the preset's proportions; build them on a throwaway skeleton once
    const h = buildHumanoid(p)
    c = buildClips(h.bones, p)
    h.dispose()
    clipCache.set(preset, c)
  }
  return c
}

function makePuppet(preset: number): Puppet {
  const h = buildHumanoid(PRESETS[preset]!)
  h.root.visible = false
  const mixer = new AnimationMixer(h.root)
  const clips = clipsFor(preset)
  const actions = {} as Record<ClipName, AnimationAction>
  for (const name of CLIP_NAMES) {
    const a = mixer.clipAction(clips[name])
    if (!CLIP_LOOPS[name]) {
      a.setLoop(LoopOnce, 1)
      a.clampWhenFinished = true
    }
    a.setEffectiveWeight(0)
    if (CLIP_LOOPS[name]) a.play()
    actions[name] = a
  }
  return {
    h, preset, mixer, actions, slot: -1, id: '', name: '', forageW: 0, forageUntil: 0, deadArmed: false,
    gesture: NO_GESTURE, faceId: null, faceUntil: 0, yaw: Number.NaN, headYaw: 0, style: makeStyle(), split: makeSplit(),
  }
}

/** Hash for the degeneration jitter (same as the capsule path). */
function hash(n: number): number {
  const x = Math.sin(n * 12.9898) * 43758.5453
  return x - Math.floor(x)
}

const WHITE = new Color('#ffffff')
const ASH = new Color('#3a3f4c')
const BLACK = new Color('#000000')
const flicker = new Color()
const tint = new Color()

// ------------------------------------------------------------ event feed
// Talk and forage are learned from the event ring at the render tick, like
// Effects.tsx does with its own queue (that queue is drained there, so this
// system keeps a second small one fed from the store's ring by absolute index).

let eventRing: EventRing | null = null
let ringCursor = 0
let lastTick = -1
const queue = new GestureQueue(BEHAVIOUR_KINDS)

function* ringEvents(): Generator<EventMsg> {
  const r = eventRing
  if (!r) return
  for (let abs = r.oldestAbs; abs < r.total; abs++) {
    const e = r.atAbs(abs)
    if (e) yield e
  }
}

function pollEvents(): void {
  const r = eventRing
  if (!r) return
  if (r.total < ringCursor) ringCursor = 0 // ring cleared (new run)
  let abs = ringCursor > r.oldestAbs ? ringCursor : r.oldestAbs
  for (; abs < r.total; abs++) {
    const e = r.atAbs(abs)
    if (e && BEHAVIOUR_KINDS.has(e.kind)) queue.push(e)
  }
  ringCursor = r.total
}

function CharacterLayer() {
  const group = useMemo(() => {
    const g = new Group()
    g.name = 'characters'
    return g
  }, [])

  useEffect(() => {
    eventRing = useStore.getState().events
    const unsub = useStore.subscribe((s) => {
      if (s.events !== eventRing) {
        eventRing = s.events
        ringCursor = 0
      }
    })
    return () => {
      unsub()
      eventRing = null
    }
  }, [])

  useEffect(() => {
    const free: Puppet[] = []
    let bySlot: Array<Puppet | null> = new Array<Puppet | null>(sampled.out.capacity).fill(null)
    /** ctx.now until which the agent in each slot counts as speaking (for glances). */
    let speakingUntil = new Float64Array(sampled.out.capacity)
    let assignedCount = 0
    let curNow = 0

    const acquire = (preset: number, slot: number, id: string, name: string): Puppet => {
      let pup: Puppet | undefined
      for (let i = 0; i < free.length; i++) {
        if (free[i]!.preset === preset) {
          pup = free[i]!
          free[i] = free[free.length - 1]!
          free.pop()
          break
        }
      }
      if (!pup) {
        pup = makePuppet(preset)
        group.add(pup.h.root)
      }
      pup.slot = slot
      pup.id = id
      pup.name = name
      pup.forageW = 0
      pup.forageUntil = 0
      pup.deadArmed = false
      pup.gesture = NO_GESTURE
      pup.faceId = null
      pup.faceUntil = 0
      pup.yaw = Number.NaN
      pup.headYaw = 0
      pup.h.root.visible = true
      pup.h.root.userData.slot = slot
      for (const name of CLIP_NAMES) {
        const a = pup.actions[name]
        a.reset()
        a.setEffectiveWeight(0)
        if (CLIP_LOOPS[name]) a.play()
        else a.stop()
      }
      pup.mixer.setTime(perSlot.phase[slot]! * 4) // desynchronise idle cycles across agents
      bySlot[slot] = pup
      assignedCount++
      return pup
    }
    const release = (pup: Puppet) => {
      bySlot[pup.slot] = null
      pup.slot = -1
      pup.id = ''
      pup.h.root.visible = false
      pup.h.root.userData.slot = -1
      free.push(pup)
      assignedCount--
    }
    const releaseAll = () => {
      for (let s = 0; s < bySlot.length; s++) if (bySlot[s]) release(bySlot[s]!)
    }

    /** Install a new gesture state, starting the clip of a one-shot that just became current. */
    const setGesture = (pup: Puppet, next: GestureState) => {
      const cur = next.current
      if (cur !== null && cur !== pup.gesture.current && !CLIP_LOOPS[cur.clip]) {
        const a = pup.actions[cur.clip]
        a.reset()
        a.setEffectiveWeight(0)
        a.play()
      }
      pup.gesture = next
    }

    const fire = (e: EventMsg) => {
      if (isEventKind(e, 'forage')) {
        const s = history.agentIndex.get(e.payload.agent_id)
        const pup = s === undefined ? null : bySlot[s]
        if (pup) pup.forageUntil = curNow + FORAGE_HOLD
        return
      }
      if (isEventKind(e, 'talk')) {
        const s = history.agentIndex.get(e.payload.speaker_id)
        if (s !== undefined && s < speakingUntil.length) speakingUntil[s] = curNow + SPEAK_GLANCE
      }
      const gestures = gestureFor(e)
      for (let i = 0; i < gestures.length; i++) {
        const g = gestures[i]!
        const s = history.agentIndex.get(g.agentId)
        const pup = s === undefined ? null : bySlot[s]
        if (!pup) continue
        if (g.faceId !== undefined) {
          pup.faceId = g.faceId
          pup.faceUntil = curNow + (g.faceFor ?? FACE_DEFAULT)
        }
        setGesture(pup, applyGesture(pup.gesture, g, curNow))
      }
    }

    const update = (pup: Puppet, s: number, ctx: FrameCtx) => {
      const { out, dt, now, speed, reducedMotion } = ctx
      const id = pup.id
      const w = perSlot.weights
      const b = s * 5
      const wi = w[b + W_IDLE]!
      const ww = w[b + W_WALK]!
      const ws = w[b + W_SLEEP]!
      const wd = w[b + W_DEGEN]!
      const wx = w[b + W_DEAD]!
      const x0 = out.x[s]!
      const z0 = out.y[s]!
      const ground = ctx.heightAt(x0, z0)
      const birth = out.scale[s]!
      const sc = birth * (0.35 + 0.65 * Math.sin((Math.min(1, birth) * Math.PI) / 2))
      const alpha = out.alpha[s]!
      let x = x0
      let z = z0
      let y = ground
      if (wd > 0.001 && !reducedMotion) {
        x += (hash(now * 41 + s) - 0.5) * 0.12 * wd
        z += (hash(now * 37 + s * 3) - 0.5) * 0.12 * wd
        y += (hash(now * 53 + s * 7) - 0.5) * 0.05 * wd
      }
      const root = pup.h.root
      root.position.set(x, y, z)
      root.scale.setScalar(sc)
      const vel = Math.hypot(out.vx[s]!, out.vy[s]!) * speed
      const standing = vel < STAND_VEL && wx < 0.5 && ws < 0.5

      // facing: a standing agent turns to whoever its gesture named, then back to its snapshot heading
      const heading = out.heading[s]!
      if (Number.isNaN(pup.yaw) || !standing) pup.yaw = heading
      let faceTarget = heading
      let faceRate = RETURN_RATE
      if (standing && pup.faceId !== null && now < pup.faceUntil) {
        const ts = history.agentIndex.get(pup.faceId)
        if (ts !== undefined && ts < out.count && out.present[ts] && out.ids[ts] === pup.faceId) {
          const tx = out.x[ts]!
          const tz = out.y[ts]!
          const d = Math.hypot(tx - x0, tz - z0)
          if (d > 1e-3 && d <= FACE_RANGE) {
            faceTarget = targetYawTo(x0, z0, tx, tz)
            faceRate = FACE_RATE
          }
        }
      } else if (now >= pup.faceUntil) pup.faceId = null
      pup.yaw = facingYaw(pup.yaw, faceTarget, dt, faceRate)
      root.rotation.set(0, Math.PI / 2 - pup.yaw, 0)

      // forage: loops while a forage happened recently (or the last action is a forage) and the agent stands
      const stat = useStore.getState().agentStats.get(id)
      const foraging =
        pup.forageUntil > now ||
        (stat !== undefined && stat.lastAction !== null && stat.lastAction.type === 'forage' && Math.abs(stat.tick - out.tick) <= 2)
      const wantForage = foraging && vel < 0.05 && wi > 0.5 ? 1 : 0
      pup.forageW += (wantForage - pup.forageW) * (1 - Math.exp(-dt / 0.25))
      const fw = pup.forageW

      // gestures: the dead and the sleeping do not gesture; walking cancels the standing holds
      const A = pup.actions
      if (wx > 0.5 || ws > 0.5) {
        if (pup.gesture !== NO_GESTURE) pup.gesture = NO_GESTURE
      } else if (ww > 0.5) {
        const next = cancelStandingHolds(pup.gesture, now)
        if (next !== pup.gesture) setGesture(pup, next)
      }
      let gw = 0
      let cur = pup.gesture.current
      if (cur !== null) {
        if (CLIP_LOOPS[cur.clip]) {
          const t = now - cur.start
          if (t >= cur.hold) setGesture(pup, finishCurrent(pup.gesture, now))
          else gw = envelope(t, cur.hold, HOLD_ATTACK, HOLD_RELEASE)
        } else {
          const a = A[cur.clip]
          const d = CLIP_DURATION[cur.clip]
          if ((!a.isRunning() && a.time > 0) || now - cur.start > d + SHOT_GRACE) {
            if (a.paused) a.stop()
            setGesture(pup, finishCurrent(pup.gesture, now))
          } else gw = envelope(a.time, d, SHOT_ATTACK, SHOT_RELEASE)
        }
        cur = pup.gesture.current
      }
      gw *= gestureScale(ww, reducedMotion) * (1 - ws)

      // personality: split idle and walk by style, time scales from curiosity and industriousness
      const style = styleFor(mirror.rosterById.get(id)?.traits, out.stress[s]!, pup.style)
      const sp = splitBase(wi, ww, style, pup.split)
      const g = Math.max(gw, fw)
      A.idle.setEffectiveWeight(sp.idle * (1 - g))
      A.idle_fidget.setEffectiveWeight(sp.fidget * (1 - g))
      A.idle_guard.setEffectiveWeight(sp.guard * (1 - g))
      A.walk.setEffectiveWeight(sp.walk * (1 - g))
      A.walk_brisk.setEffectiveWeight(sp.brisk * (1 - g))
      A.sleep.setEffectiveWeight(ws * (1 - g))
      A.degenerate.setEffectiveWeight(wd * (1 - g))
      A.dead.setEffectiveWeight(wx * (1 - gw))
      A.forage.setEffectiveWeight(fw * (1 - gw))
      for (let i = 0; i < GESTURES.length; i++) {
        const name = GESTURES[i]!
        A[name].setEffectiveWeight(cur !== null && cur.clip === name ? gw : 0)
      }
      A.idle.setEffectiveTimeScale(style.idleTimeScale)
      A.idle_fidget.setEffectiveTimeScale(style.idleTimeScale)
      A.idle_guard.setEffectiveTimeScale(style.idleTimeScale)
      if (ww > 0.001) {
        const ts = walkTimeScale(vel, style)
        A.walk.setEffectiveTimeScale(ts)
        // the brisk loop is shorter: run it faster by the ratio and lock its phase to the walk so the legs agree
        A.walk_brisk.setEffectiveTimeScale(ts / BRISK_RATIO)
        A.walk_brisk.time = (A.walk.time / CLIP_DURATION.walk) * CLIP_DURATION.walk_brisk
      }
      // the collapse plays once when the agent dies (dead clip clamps at its end)
      if (wx > 0.02) {
        if (!pup.deadArmed) {
          pup.deadArmed = true
          A.dead.reset()
          A.dead.setEffectiveWeight(wx)
          A.dead.play()
        }
      } else pup.deadArmed = false

      // materials: tint, sleep dim, ash when dead, highlight, degeneration flicker, alpha fade
      const base = slotColor(s, id)
      const m = pup.h.materials
      m.body.color.copy(WHITE).multiplyScalar(1 - 0.3 * ws)
      if (wx > 0.001) m.body.color.lerp(ASH, 0.6 * wx)
      m.body.emissive.copy(BLACK)
      if (id === mirror.selectedId) m.body.emissive.copy(base).multiplyScalar(0.28)
      else if (id === mirror.hoveredId) m.body.emissive.copy(base).multiplyScalar(0.14)
      if (wd > 0.001) {
        const f = reducedMotion ? 0.5 : 0.5 + 0.5 * Math.sin(now * 23 + s)
        flicker.setHSL((now * 0.7 + s * 0.13) % 1, 0.9, 0.5)
        m.body.emissive.lerp(flicker, wd * 0.5 * f)
      }
      const flash = perSlot.flashUntil[s]! > now ? Math.min(1, perSlot.flashUntil[s]! - now) : 0
      if (flash > 0) m.body.emissive.lerp(WHITE, 0.35 * flash)
      const opaque = alpha > 0.995
      m.body.opacity = alpha
      m.body.transparent = !opaque
      m.body.depthWrite = true
      tint.copy(base)
      if (id === mirror.selectedId) tint.lerp(WHITE, 0.3)
      m.trim.color.copy(tint)
      m.trim.emissive.copy(tint)
      m.trim.emissiveIntensity = (0.45 + 0.4 * flash + 0.25 * out.stress[s]!) * (1 - 0.6 * ws) * (1 - 0.8 * wx)
      m.trim.opacity = alpha
      m.trim.transparent = !opaque

      pup.mixer.update(dt * (speed <= 0 ? 1 : Math.max(0.5, Math.min(2, speed))))

      // post-mixer: an idle agent glances at a nearby speaker; industrious agents lean into their work
      let glance = 0
      if (standing && wi > 0.5 && gw < 0.5) {
        let best = GLANCE_RANGE
        for (let o = 0; o < out.count && o < speakingUntil.length; o++) {
          if (o === s || !out.present[o] || speakingUntil[o]! <= now) continue
          const d = Math.hypot(out.x[o]! - x0, out.y[o]! - z0)
          if (d < best && d > 1e-3) {
            best = d
            glance = headYawFor(x0, z0, pup.yaw, out.x[o]!, out.y[o]!)
          }
        }
      }
      pup.headYaw += (glance - pup.headYaw) * (1 - Math.exp(-dt * HEAD_RATE))
      if (Math.abs(pup.headYaw) > 1e-4) pup.h.bones.head.quaternion.multiply(qScratch.setFromAxisAngle(Y_AXIS, pup.headYaw * (1 - ws)))
      if (!reducedMotion && Math.abs(style.leanForward) > 1e-4) {
        pup.h.bones.spine.quaternion.multiply(qScratch.setFromAxisAngle(X_AXIS, style.leanForward * (wi + ww) * (1 - wx)))
      }
    }

    const system = (ctx: FrameCtx) => {
      const { out, now } = ctx
      curNow = now
      if (rigState.glbActive) {
        if (assignedCount > 0) releaseAll()
        return
      }
      if (bySlot.length < out.capacity) {
        const grown = new Array<Puppet | null>(out.capacity).fill(null)
        for (let i = 0; i < bySlot.length; i++) grown[i] = bySlot[i]!
        bySlot = grown
        const until = new Float64Array(out.capacity)
        until.set(speakingUntil)
        speakingUntil = until
      }
      // scrubbing backwards re-arms the effects we will see again
      if (lastTick >= 0 && out.tick < lastTick - 1) queue.reseed(ringEvents(), out.tick)
      lastTick = out.tick
      pollEvents()

      rigState.riggedSlots.clear()
      for (let s = 0; s < bySlot.length; s++) {
        const pup = bySlot[s]
        if (pup && (s >= out.count || !out.present[s] || out.ids[s] !== pup.id)) release(pup)
      }
      for (let s = 0; s < out.count; s++) {
        if (!out.present[s]) continue
        const id = out.ids[s]!
        const name = mirror.rosterById.get(id)?.name ?? id
        let pup = bySlot[s]
        if (!pup) {
          if (assignedCount >= rigState.maxRigged) continue
          pup = acquire(presetIndexFor(name), s, id, name)
        } else if (name !== pup.name) {
          // the roster arrived after the first snapshot: the preset may change with the real name
          const pi = presetIndexFor(name)
          if (pi !== pup.preset) {
            release(pup)
            pup = acquire(pi, s, id, name)
          } else pup.name = name
        }
        rigState.riggedSlots.add(s)
        update(pup, s, ctx)
      }
      queue.drain(out.tick, fire)
    }
    const unregister = registerSystem(SYS_RIG - 1, system)
    return () => {
      unregister()
      releaseAll()
      rigState.riggedSlots.clear()
      for (const pup of free) {
        pup.mixer.stopAllAction()
        group.remove(pup.h.root)
        pup.h.dispose()
      }
      free.length = 0
      queue.clear()
      lastTick = -1
    }
  }, [group])

  const slotOf = (o: Object3D | null): number => {
    let cur: Object3D | null = o
    while (cur && cur !== group) {
      const s = cur.userData.slot
      if (typeof s === 'number') return s
      cur = cur.parent
    }
    return -1
  }
  const idAt = (o: Object3D | null): string | null => {
    const s = slotOf(o)
    if (s < 0) return null
    const id = sampled.out.ids[s]
    return id ? id : null
  }
  const onMove = (e: ThreeEvent<PointerEvent>) => {
    const id = idAt(e.object)
    if (!id) return
    e.stopPropagation()
    useStore.getState().hover(id)
  }
  const onOut = () => useStore.getState().hover(null)
  const onClick = (e: ThreeEvent<MouseEvent>) => {
    const id = idAt(e.object)
    if (!id) return
    e.stopPropagation()
    useStore.getState().select(id)
  }
  const onDouble = (e: ThreeEvent<MouseEvent>) => {
    const id = idAt(e.object)
    if (!id) return
    e.stopPropagation()
    const st = useStore.getState()
    st.select(id)
    st.follow(id)
  }
  return <primitive object={group} onPointerMove={onMove} onPointerOut={onOut} onClick={onClick} onDoubleClick={onDouble} />
}

/** Mounts the procedural character layer unless characters are disabled for this page. */
export function CharacterSystem() {
  const enabled = useMemo(() => charactersEnabled(), [])
  if (!enabled) return null
  return <CharacterLayer />
}
