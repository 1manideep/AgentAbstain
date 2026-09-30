import { useEffect, useMemo, useRef, useState } from 'react'
import { CapsuleGeometry, Color, DynamicDrawUsage, InstancedMesh, Object3D } from 'three'
import type { ThreeEvent } from '@react-three/fiber'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { attachInstanceAttributes, makeInstancedMaterial } from './instancedMaterial'
import { mirror, perSlot, registerSystem, sampled, slotColor, SYS_AGENTS, type FrameCtx } from './sceneState'

const W_IDLE = 0
const W_WALK = 1
const W_SLEEP = 2
const W_DEGEN = 3
const W_DEAD = 4
const TAU = Math.PI * 2

const dummy = new Object3D()
const color = new Color()
const flicker = new Color()

/** Cheap deterministic noise for degeneration jitter. */
function hash(n: number): number {
  const x = Math.sin(n * 12.9898) * 43758.5453
  return x - Math.floor(x)
}

/** Maps visible instance index → history slot, for picking. */
export const instToSlot = { map: new Int32Array(64), count: 0 }

export function Agents() {
  const [capacity, setCapacity] = useState(history.capacity)
  useEffect(() => history.onCapacityChange((cap) => setCapacity(cap)), [])

  const geometry = useMemo(() => {
    const g = new CapsuleGeometry(0.46, 0.9, 4, 12)
    g.translate(0, 0.91, 0)
    return g
  }, [])
  const material = useMemo(() => makeInstancedMaterial({ roughness: 0.5, metalness: 0.1, emissiveScale: 1.6 }), [])
  const meshRef = useRef<InstancedMesh>(null)

  const mesh = useMemo(() => {
    const m = new InstancedMesh(geometry, material, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    // instanceColor is created lazily by setColorAt; do it once so the shader defines are stable
    m.setColorAt(0, color.set('#ffffff'))
    m.instanceColor!.setUsage(DynamicDrawUsage)
    return m
  }, [geometry, material, capacity])

  const attrs = useMemo(() => attachInstanceAttributes(geometry, capacity), [geometry, capacity])

  useEffect(() => () => mesh.dispose(), [mesh])

  useEffect(() => {
    if (instToSlot.map.length < capacity) instToSlot.map = new Int32Array(capacity)
    const system = (ctx: FrameCtx) => {
      const { out, dt, now, speed, reducedMotion } = ctx
      const p = perSlot
      const k = 1 - Math.exp(-dt / 0.18)
      const selected = mirror.selectedId
      const hovered = mirror.hoveredId
      let n = 0
      const pulseArr = attrs.pulse.array as Float32Array
      const fadeArr = attrs.fade.array as Float32Array
      for (let s = 0; s < out.count; s++) {
        if (!out.present[s]) continue
        const id = out.ids[s]!
        const vel = Math.hypot(out.vx[s]!, out.vy[s]!) * speed
        const w = p.weights
        const b = s * 5
        const dead = out.dead[s] === 1
        const sleep = out.asleep[s] === 1 && !dead
        const degen = !dead && (out.degenerate[s] === 1 || p.degenUntil[s]! > now)
        const walk = !dead && !sleep && vel > 0.05
        const tIdle = walk || sleep || degen || dead ? 0 : 1
        w[b + W_IDLE]! += (tIdle - w[b + W_IDLE]!) * k
        w[b + W_WALK]! += ((walk ? 1 : 0) - w[b + W_WALK]!) * k
        w[b + W_SLEEP]! += ((sleep ? 1 : 0) - w[b + W_SLEEP]!) * k
        w[b + W_DEGEN]! += ((degen ? 1 : 0) - w[b + W_DEGEN]!) * k
        w[b + W_DEAD]! += ((dead ? 1 : 0) - w[b + W_DEAD]!) * k
        const wi = w[b + W_IDLE]!
        const ww = w[b + W_WALK]!
        const ws = w[b + W_SLEEP]!
        const wd = w[b + W_DEGEN]!
        const wx = w[b + W_DEAD]!

        // phase advances with playback so bobbing pauses when the world pauses
        p.phase[s]! += dt * (0.6 + speed * Math.min(2, vel))
        const ph = p.phase[s]!
        const stress = out.stress[s]!

        let x = out.x[s]!
        let z = out.y[s]!
        let y = 0
        // walk: bob + forward lean
        y += Math.abs(Math.sin(ph * 6)) * 0.16 * ww
        const lean = 0.16 * ww
        // idle: breathing
        let sy = 1 + 0.03 * Math.sin(now * 1.9 + s) * wi
        let sxz = 1 - 0.015 * Math.sin(now * 1.9 + s) * wi
        // sleep: squash
        sy *= 1 - 0.45 * ws
        sxz *= 1 + 0.18 * ws
        // degenerate: jitter
        if (wd > 0.001 && !reducedMotion) {
          x += (hash(now * 41 + s) - 0.5) * 0.12 * wd
          z += (hash(now * 37 + s * 3) - 0.5) * 0.12 * wd
          y += (hash(now * 53 + s * 7) - 0.5) * 0.05 * wd
        }
        // dead: slump and shrink
        sy *= 1 - 0.35 * wx
        const tilt = 0.35 * wx
        const birth = out.scale[s]!
        const sc = birth * (0.35 + 0.65 * Math.sin((Math.min(1, birth) * Math.PI) / 2)) // ease-out pop-in
        dummy.position.set(x, y, z)
        dummy.rotation.set(0, -out.heading[s]!, -lean - tilt, 'YZX')
        dummy.scale.set(sxz * sc, sy * sc, sxz * sc)
        dummy.updateMatrix()
        mesh.setMatrixAt(n, dummy.matrix)

        // colour: tier, brightened on hover/selection, hue-flickered while degenerate
        const base = slotColor(s, id)
        color.copy(base)
        if (wd > 0.001) {
          const f = reducedMotion ? 0.5 : 0.5 + 0.5 * Math.sin(now * 23 + s)
          flicker.setHSL((now * 0.7 + s * 0.13) % 1, 0.9, 0.6)
          color.lerp(flicker, wd * 0.55 * f)
        }
        if (ws > 0.001) color.multiplyScalar(1 - 0.45 * ws)
        if (id === selected) color.lerp(WHITE, 0.25)
        else if (id === hovered) color.lerp(WHITE, 0.12)
        if (wx > 0.001) color.lerp(ASH, 0.7 * wx)
        mesh.setColorAt(n, color)

        // emissive pulse: 0.5 + 2·stress Hz, amplitude stress (steady under reduced motion)
        let pulse = reducedMotion ? stress * 0.5 : stress * (0.5 + 0.5 * Math.sin(TAU * (0.5 + 2 * stress) * now + s))
        pulse *= 1 - 0.8 * ws
        if (p.flashUntil[s]! > now) pulse += (p.flashUntil[s]! - now) * 1.2
        if (id === selected) pulse += 0.12
        pulseArr[n] = pulse
        fadeArr[n] = out.alpha[s]! * (1 - 0.15 * ws)
        instToSlot.map[n] = s
        n++
      }
      instToSlot.count = n
      mesh.count = n
      if (n > 0) {
        mesh.instanceMatrix.needsUpdate = true
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
        attrs.pulse.needsUpdate = true
        attrs.fade.needsUpdate = true
      }
    }
    return registerSystem(SYS_AGENTS, system)
  }, [mesh, attrs, capacity])

  const onMove = (e: ThreeEvent<PointerEvent>) => {
    const i = e.instanceId
    if (i === undefined || i >= instToSlot.count) return
    e.stopPropagation()
    const id = sampledIdAt(instToSlot.map[i]!)
    useStore.getState().hover(id)
  }
  const onOut = () => useStore.getState().hover(null)
  const onClick = (e: ThreeEvent<MouseEvent>) => {
    const i = e.instanceId
    if (i === undefined || i >= instToSlot.count) return
    e.stopPropagation()
    const id = sampledIdAt(instToSlot.map[i]!)
    const st = useStore.getState()
    st.select(id)
  }
  const onDouble = (e: ThreeEvent<MouseEvent>) => {
    const i = e.instanceId
    if (i === undefined || i >= instToSlot.count) return
    e.stopPropagation()
    const id = sampledIdAt(instToSlot.map[i]!)
    const st = useStore.getState()
    st.select(id)
    st.follow(id)
  }

  return (
    <primitive
      ref={meshRef}
      object={mesh}
      onPointerMove={onMove}
      onPointerOut={onOut}
      onClick={onClick}
      onDoubleClick={onDouble}
    />
  )
}

const WHITE = new Color('#ffffff')
const ASH = new Color('#3a3f4c')

function sampledIdAt(slot: number): string | null {
  const id = sampled.out.ids[slot]
  return id ? id : null
}
