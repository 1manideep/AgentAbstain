/**
 * Character gallery (`#/characters`): the ten presets in a row on a lit
 * ground, each labelled, cycling through the clips every few seconds.
 *
 *   ?clip=walk      force one clip
 *   ?still=1        freeze at t = 0.4 s (`?t=1.2` picks the time)
 *   ?moves=1        one preset (`?preset=3`) seven times, one per clip, each
 *                   frozen at a representative moment (the "moves" strip)
 *   ?tier=4fd1c5    trim colour (hex without #)
 *
 * Query parameters may sit before or after the hash.
 */
import { Text } from '@react-three/drei'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { AnimationMixer, Group, LoopOnce, type AnimationAction } from 'three'
import { gpuProfile } from '../gpu'
import { buildClips, CLIP_DURATION, CLIP_LOOPS, CLIP_NAMES, type ClipName } from './clips'
import { buildHumanoid, type Humanoid } from './humanoid'
import { PRESETS } from './presets'

const FONT_URL = '/fonts/InstrumentSans-Regular.ttf'
const CYCLE_S = 3
const TIERS = ['#4fd1c5', '#f6ad55', '#a0aec0']
/** Representative freeze time per clip for the moves strip. */
const MOVE_T: Record<ClipName, number> = { idle: 2.9, walk: 0.1, sleep: 1.0, degenerate: 0.62, dead: 1.4, talk: 0.55, forage: 0.75 }

interface Params {
  clip: ClipName | null
  still: boolean
  t: number
  moves: boolean
  preset: number
  tier: string | null
}

function isClip(v: string | null): v is ClipName {
  return v !== null && (CLIP_NAMES as readonly string[]).includes(v)
}

function readParams(): Params {
  const q = new URLSearchParams(window.location.search)
  const hash = window.location.hash
  const i = hash.indexOf('?')
  if (i >= 0) for (const [k, v] of new URLSearchParams(hash.slice(i + 1))) q.set(k, v)
  const clip = q.get('clip')
  const t = Number(q.get('t'))
  const preset = Number(q.get('preset'))
  const tier = q.get('tier')
  return {
    clip: isClip(clip) ? clip : null,
    still: q.get('still') === '1',
    t: Number.isFinite(t) && q.has('t') ? t : 0.4,
    moves: q.get('moves') === '1',
    preset: Number.isFinite(preset) && preset >= 0 && preset < PRESETS.length ? Math.floor(preset) : 3,
    tier: tier && /^[0-9a-f]{6}$/i.test(tier) ? '#' + tier : null,
  }
}

interface Entry {
  label: string
  x: number
  h: Humanoid
  mixer: AnimationMixer
  actions: Record<ClipName, AnimationAction>
  /** Fixed clip (moves strip) or null to follow the cycle. */
  fixed: ClipName | null
  freezeT: number
  current: ClipName | null
  frozen: boolean
  yaw: number
}

function makeEntry(presetIdx: number, x: number, label: string, fixed: ClipName | null, freezeT: number, tier: string): Entry {
  const preset = PRESETS[presetIdx]!
  const h = buildHumanoid(preset)
  const clips = buildClips(h.bones, preset)
  const mixer = new AnimationMixer(h.root)
  const actions = {} as Record<ClipName, AnimationAction>
  for (const name of CLIP_NAMES) {
    const a = mixer.clipAction(clips[name])
    if (!CLIP_LOOPS[name]) {
      a.setLoop(LoopOnce, 1)
      a.clampWhenFinished = true
    }
    actions[name] = a
  }
  h.materials.trim.color.set(tier)
  h.materials.trim.emissive.set(tier)
  h.root.position.set(x, 0, 0)
  return { label, x, h, mixer, actions, fixed, freezeT, current: null, frozen: false, yaw: 0 }
}

/** Start `clip` on an entry, crossfading from whatever played before. */
function play(e: Entry, clip: ClipName, fade: number): void {
  if (e.current === clip) return
  const next = e.actions[clip]
  if (e.current) {
    const prev = e.actions[e.current]
    if (fade > 0) {
      prev.fadeOut(fade)
      next.reset().setEffectiveWeight(1).fadeIn(fade).play()
    } else {
      prev.stop()
      next.reset().setEffectiveWeight(1).play()
    }
  } else next.reset().setEffectiveWeight(1).play()
  e.current = clip
}

interface Slot {
  label: string
  x: number
  height: number
}

/** Static layout: positions, labels and heights derive from the params alone. */
function layoutFor(params: Params): { spacing: number; width: number; slots: Slot[] } {
  const slots: Slot[] = []
  if (params.moves) {
    const spacing = 2.25
    const n = CLIP_NAMES.length
    const h = PRESETS[params.preset]!.height
    for (let i = 0; i < n; i++) slots.push({ label: CLIP_NAMES[i]!, x: (i - (n - 1) / 2) * spacing, height: h })
    return { spacing, width: spacing * (n - 1) + 3.2, slots }
  }
  const spacing = 1.38
  const n = PRESETS.length
  for (let i = 0; i < n; i++) slots.push({ label: PRESETS[i]!.name, x: (i - (n - 1) / 2) * spacing, height: PRESETS[i]!.height })
  return { spacing, width: spacing * (n - 1) + 2.2, slots }
}

function Characters({ params }: { params: Params }) {
  const group = useMemo(() => new Group(), [])
  const entries = useRef<Entry[]>([])
  const { camera, size } = useThree()
  const layout = useMemo(() => layoutFor(params), [params])

  useEffect(() => {
    const list: Entry[] = []
    layout.slots.forEach((slot, i) => {
      const tier = params.tier ?? TIERS[i % TIERS.length]!
      if (params.moves) {
        const clip = CLIP_NAMES[i]!
        list.push(makeEntry(params.preset, slot.x, clip, clip, MOVE_T[clip], tier))
      } else list.push(makeEntry(i, slot.x, slot.label, null, params.t, tier))
    })
    for (const e of list) group.add(e.h.root)
    const holder = entries
    holder.current = list
    return () => {
      for (const e of list) {
        e.mixer.stopAllAction()
        group.remove(e.h.root)
        e.h.dispose()
      }
      holder.current = []
    }
  }, [group, layout, params])

  useEffect(() => {
    const aspect = size.width / Math.max(1, size.height)
    const fov = 30
    const half = layout.width / 2
    const dist = half / (Math.tan((fov * Math.PI) / 360) * aspect) + 1.5
    camera.position.set(0, 1.9, Math.max(6, dist))
    camera.lookAt(0, 0.95, 0)
    camera.updateProjectionMatrix()
  }, [camera, size.width, size.height, layout])

  useFrame((state, delta) => {
    const dt = Math.min(0.1, delta)
    const cycle = params.clip ?? CLIP_NAMES[Math.floor(state.clock.elapsedTime / CYCLE_S) % CLIP_NAMES.length]!
    for (const e of entries.current) {
      const clip = e.fixed ?? cycle
      const freeze = params.still || e.fixed !== null
      if (freeze) {
        if (!e.frozen || e.current !== clip) {
          play(e, clip, 0)
          e.mixer.setTime(0)
          e.actions[clip].reset().setEffectiveWeight(1).play()
          e.mixer.setTime(Math.min(e.freezeT, CLIP_DURATION[clip]))
          e.frozen = true
        }
      } else {
        play(e, clip, 0.35)
        e.mixer.update(dt)
      }
      // sleepers lie along x: in the tight row turn them toward the camera so neighbours do not overlap,
      // in the moves strip angle them a little so the curl reads from the side
      const yawTarget = clip === 'sleep' ? (params.moves ? 0.45 : Math.PI / 2) : 0
      e.yaw = freeze ? yawTarget : e.yaw + (yawTarget - e.yaw) * (1 - Math.exp(-dt * 6))
      e.h.root.rotation.y = e.yaw
    }
  })

  return (
    <group>
      <primitive object={group} />
      {layout.slots.map((slot) => (
        <group key={slot.label + slot.x}>
          <Text font={FONT_URL} fontSize={0.2} color="#eef1f8" anchorX="center" anchorY="bottom" outlineWidth={0.012} outlineColor="#0a0d14" position={[slot.x, slot.height + 0.32, 0]}>
            {slot.label}
          </Text>
          <mesh position={[slot.x, 0.005, 0.05]} rotation-x={-Math.PI / 2} renderOrder={1}>
            <circleGeometry args={[0.55, 20]} />
            <meshBasicMaterial color="#000000" transparent opacity={0.28} depthWrite={false} />
          </mesh>
        </group>
      ))}
    </group>
  )
}

export function Gallery() {
  const params = useMemo(() => readParams(), [])
  const caption = params.moves ? `${PRESETS[params.preset]!.name} · ${PRESETS[params.preset]!.description}` : params.clip ?? (params.still ? 'still' : 'cycling clips')
  return (
    <div style={{ position: 'absolute', inset: 0, background: '#1c2130', color: '#dfe4ee', fontFamily: 'Instrument Sans, system-ui, sans-serif' }}>
      <Canvas frameloop="always" dpr={1} gl={{ antialias: gpuProfile.antialias, alpha: false, stencil: false }} camera={{ fov: 30, near: 0.1, far: 200, position: [0, 1.9, 14] }} style={{ position: 'absolute', inset: 0 }}>
        <color attach="background" args={['#242a3b']} />
        <fog attach="fog" args={['#242a3b', 30, 70]} />
        <hemisphereLight args={['#dfe7ff', '#3a3f4a', 0.9]} />
        <directionalLight position={[5, 9, 7]} intensity={2.4} color="#fff1dd" />
        <directionalLight position={[-7, 4, -5]} intensity={0.7} color="#9fb4ff" />
        <mesh rotation-x={-Math.PI / 2} position-y={0}>
          <planeGeometry args={[80, 40]} />
          <meshStandardMaterial color="#4a5268" roughness={0.95} />
        </mesh>
        <Characters params={params} />
      </Canvas>
      <div style={{ position: 'absolute', left: 16, bottom: 12, fontSize: 13, opacity: 0.8 }}>
        characters · {caption}
        {params.still ? ` · t=${params.t}s` : ''}
      </div>
    </div>
  )
}
