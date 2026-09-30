/**
 * Draws visible agents as procedural humanoids (see humanoid.ts / clips.ts)
 * in place of the instanced capsule bodies. Runs as a scene system at SYS_RIG:
 * up to `rigState.maxRigged` present slots get a puppet (a humanoid for the
 * agent's preset, an AnimationMixer and one action per clip); the per-slot
 * weight vector the capsule path computes (perSlot.weights: idle, walk, sleep,
 * degenerate, dead) drives the clip weights, `talk` is a one-shot fired from
 * talk events at the render tick, and `forage` loops while the agent forages
 * standing still. Slots drawn here are marked in `rigState.riggedSlots` so
 * Agents.tsx skips their capsule (the shadow blob and halo are still drawn
 * there). A loaded GLB rig (Rig.tsx) takes precedence: while it is active the
 * puppets stay hidden. Nothing here allocates per frame in steady state.
 */
import type { ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import { AnimationMixer, Color, Group, LoopOnce, type AnimationAction, type AnimationClip, type Object3D } from 'three'
import { isEventKind, type EventMsg } from '../../protocol'
import type { EventRing } from '../../state/events'
import { history } from '../../state/history'
import { useStore } from '../../state/store'
import { FxQueue } from '../fx'
import { gpuProfile } from '../gpu'
import { rigState } from '../rigState'
import { mirror, perSlot, registerSystem, sampled, slotColor, SYS_RIG, type FrameCtx } from '../sceneState'
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
/** World units covered by one walk cycle at timeScale 1. */
const STRIDE = 1.3

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
    if (name !== 'talk') a.play()
    actions[name] = a
  }
  return { h, preset, mixer, actions, slot: -1, id: '', name: '', forageW: 0, forageUntil: 0, deadArmed: false }
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
const queue = new FxQueue()

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
    if (e && (e.kind === 'talk' || e.kind === 'forage')) queue.push(e)
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
      pup.h.root.visible = true
      pup.h.root.userData.slot = slot
      for (const name of CLIP_NAMES) {
        const a = pup.actions[name]
        a.reset()
        a.setEffectiveWeight(0)
        if (name !== 'talk') a.play()
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

    const fire = (e: EventMsg) => {
      if (isEventKind(e, 'talk')) {
        const s = history.agentIndex.get(e.payload.speaker_id)
        const pup = s === undefined ? null : bySlot[s]
        if (!pup) return
        const talk = pup.actions.talk
        if (!talk.isRunning()) {
          talk.reset()
          talk.setEffectiveWeight(0)
          talk.play()
        }
        return
      }
      if (isEventKind(e, 'forage')) {
        const s = history.agentIndex.get(e.payload.agent_id)
        const pup = s === undefined ? null : bySlot[s]
        if (pup) pup.forageUntil = curNow + FORAGE_HOLD
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
      root.rotation.set(0, Math.PI / 2 - out.heading[s]!, 0)
      root.scale.setScalar(sc)

      // forage: loops while a forage happened recently (or the last action is a forage) and the agent stands
      const vel = Math.hypot(out.vx[s]!, out.vy[s]!) * speed
      const stat = useStore.getState().agentStats.get(id)
      const foraging =
        pup.forageUntil > now ||
        (stat !== undefined && stat.lastAction !== null && stat.lastAction.type === 'forage' && Math.abs(stat.tick - out.tick) <= 2)
      const wantForage = foraging && vel < 0.05 && wi > 0.5 ? 1 : 0
      pup.forageW += (wantForage - pup.forageW) * (1 - Math.exp(-dt / 0.25))
      const fw = pup.forageW

      // talk: one-shot envelope from the action's own time
      const talk = pup.actions.talk
      let tw = 0
      if (talk.isRunning()) {
        const t = talk.time
        const d = CLIP_DURATION.talk
        tw = Math.max(0, Math.min(1, t / 0.15, (d - t) / 0.25))
      } else if (talk.paused) talk.stop()

      const g = Math.max(tw, fw)
      const A = pup.actions
      A.idle.setEffectiveWeight(wi * (1 - g))
      A.walk.setEffectiveWeight(ww * (1 - g))
      A.sleep.setEffectiveWeight(ws * (1 - g))
      A.degenerate.setEffectiveWeight(wd * (1 - g))
      A.dead.setEffectiveWeight(wx * (1 - tw))
      A.forage.setEffectiveWeight(fw * (1 - tw))
      A.talk.setEffectiveWeight(tw)
      if (ww > 0.001) A.walk.setEffectiveTimeScale(Math.max(0.55, Math.min(1.9, vel / STRIDE)))
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
