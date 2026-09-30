import { useEffect, useMemo, useRef, useState } from 'react'
import { CapsuleGeometry, CircleGeometry, Color, DynamicDrawUsage, InstancedMesh, MeshBasicMaterial, Object3D, SphereGeometry, TorusGeometry, type BufferGeometry } from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { nonIndexed } from './geom'
import type { ThreeEvent } from '@react-three/fiber'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { attachInstanceAttributes, makeInstancedMaterial } from './instancedMaterial'
import { rigState } from './rigState'
import { mirror, perSlot, registerSystem, sampled, slotColor, slotProminence, SYS_AGENTS, type FrameCtx } from './sceneState'

const W_IDLE = 0
const W_WALK = 1
const W_SLEEP = 2
const W_DEGEN = 3
const W_DEAD = 4
const TAU = Math.PI * 2
export const AGENT_HEIGHT = 1.95
export const HALO_HEIGHT = 2.45

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

/** Capsule body with a head sphere: one merged geometry, base on y = 0. */
function bodyGeometry(): BufferGeometry {
  const body = new CapsuleGeometry(0.4, 0.62, 4, 12)
  body.translate(0, 0.4 + 0.31, 0) // capsule centre → base at 0, top at 1.42
  const head = new SphereGeometry(0.34, 14, 10)
  head.translate(0, 1.42 + 0.02 + 0.34, 0) // sits on a thin neck gap, top at ~2.12
  const merged = mergeGeometries([nonIndexed(body), nonIndexed(head)], false)!
  merged.computeVertexNormals()
  merged.computeBoundingSphere()
  body.dispose()
  head.dispose()
  return merged
}

export function Agents() {
  const [capacity, setCapacity] = useState(history.capacity)
  useEffect(() => history.onCapacityChange((cap) => setCapacity(cap)), [])

  const geometry = useMemo(() => bodyGeometry(), [])
  const haloGeom = useMemo(() => {
    const g = new TorusGeometry(0.5, 0.045, 8, 28)
    g.rotateX(Math.PI / 2)
    return g
  }, [])
  const blobGeom = useMemo(() => {
    const g = new CircleGeometry(0.72, 18)
    g.rotateX(-Math.PI / 2)
    return g
  }, [])
  const material = useMemo(() => makeInstancedMaterial({ roughness: 0.62, metalness: 0.04, emissiveScale: 1.6, rim: true, color: '#efe7da' }), [])
  const haloMat = useMemo(() => makeInstancedMaterial({ roughness: 0.3, metalness: 0.2, emissiveScale: 2.2 }), [])
  const blobMat = useMemo(() => new MeshBasicMaterial({ color: '#000000', transparent: true, opacity: 0.32, depthWrite: false }), [])
  const meshRef = useRef<InstancedMesh>(null)

  const mesh = useMemo(() => {
    const m = new InstancedMesh(geometry, material, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    m.setColorAt(0, color.set('#ffffff'))
    m.instanceColor!.setUsage(DynamicDrawUsage)
    return m
  }, [geometry, material, capacity])
  const halos = useMemo(() => {
    const m = new InstancedMesh(haloGeom, haloMat, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    m.setColorAt(0, color.set('#ffffff'))
    m.instanceColor!.setUsage(DynamicDrawUsage)
    return m
  }, [haloGeom, haloMat, capacity])
  const blobs = useMemo(() => {
    const m = new InstancedMesh(blobGeom, blobMat, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    m.renderOrder = 2
    return m
  }, [blobGeom, blobMat, capacity])

  const attrs = useMemo(() => attachInstanceAttributes(geometry, capacity), [geometry, capacity])
  const haloAttrs = useMemo(() => attachInstanceAttributes(haloGeom, capacity), [haloGeom, capacity])

  useEffect(
    () => () => {
      mesh.dispose()
      halos.dispose()
      blobs.dispose()
    },
    [mesh, halos, blobs],
  )

  useEffect(() => {
    if (instToSlot.map.length < capacity) instToSlot.map = new Int32Array(capacity)
    const system = (ctx: FrameCtx) => {
      const { out, dt, now, speed, reducedMotion } = ctx
      const p = perSlot
      const k = 1 - Math.exp(-dt / 0.18)
      const selected = mirror.selectedId
      const hovered = mirror.hoveredId
      let n = 0
      let nb = 0
      const pulseArr = attrs.pulse.array as Float32Array
      const fadeArr = attrs.fade.array as Float32Array
      const hPulse = haloAttrs.pulse.array as Float32Array
      const hFade = haloAttrs.fade.array as Float32Array
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
        if (p.colorId[s] !== id) {
          // the slot changed hands (a tombstone replaced by a newborn): start from the new
          // occupant's state instead of blending out of the previous one's pose
          w[b + W_IDLE] = tIdle
          w[b + W_WALK] = walk ? 1 : 0
          w[b + W_SLEEP] = sleep ? 1 : 0
          w[b + W_DEGEN] = degen ? 1 : 0
          w[b + W_DEAD] = dead ? 1 : 0
          p.phase[s] = 0
          p.degenUntil[s] = 0
          p.flashUntil[s] = 0
        }
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

        p.phase[s]! += dt * (0.6 + speed * Math.min(2, vel))
        const ph = p.phase[s]!
        const stress = out.stress[s]!
        const x0 = out.x[s]!
        const z0 = out.y[s]!
        const ground = ctx.heightAt(x0, z0)
        const birth = out.scale[s]!
        const sc = birth * (0.35 + 0.65 * Math.sin((Math.min(1, birth) * Math.PI) / 2))
        const alpha = out.alpha[s]!

        // shadow blob for every visible agent (rigged or not)
        const bob = Math.abs(Math.sin(ph * 6)) * 0.16 * ww
        dummy.position.set(x0, ground + 0.03, z0)
        dummy.rotation.set(0, 0, 0)
        const bs = sc * alpha * (1 - 0.25 * bob) * (1 + 0.15 * ws)
        dummy.scale.set(bs, 1, bs)
        dummy.updateMatrix()
        blobs.setMatrixAt(nb, dummy.matrix)
        nb++

        // a rig (GLB or procedural human) draws this agent's body; the halo is still ours, so the
        // capsule instance is kept in the buffer at zero scale to keep instance ids aligned
        const rigged = rigState.riggedSlots.has(s)

        let x = x0
        let z = z0
        let y = ground + bob
        const lean = 0.16 * ww
        let sy = 1 + 0.03 * Math.sin(now * 1.9 + s) * wi
        let sxz = 1 - 0.015 * Math.sin(now * 1.9 + s) * wi
        sy *= 1 - 0.45 * ws
        sxz *= 1 + 0.18 * ws
        if (wd > 0.001 && !reducedMotion) {
          x += (hash(now * 41 + s) - 0.5) * 0.12 * wd
          z += (hash(now * 37 + s * 3) - 0.5) * 0.12 * wd
          y += (hash(now * 53 + s * 7) - 0.5) * 0.05 * wd
        }
        sy *= 1 - 0.35 * wx
        const tilt = 0.35 * wx
        dummy.position.set(x, y, z)
        dummy.rotation.set(0, -out.heading[s]!, -lean - tilt, 'YZX')
        if (rigged) dummy.scale.set(0, 0, 0)
        else dummy.scale.set(sxz * sc, sy * sc, sxz * sc)
        dummy.updateMatrix()
        mesh.setMatrixAt(n, dummy.matrix)

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

        let pulse = reducedMotion ? stress * 0.5 : stress * (0.5 + 0.5 * Math.sin(TAU * (0.5 + 2 * stress) * now + s))
        pulse *= 1 - 0.8 * ws
        if (p.flashUntil[s]! > now) pulse += (p.flashUntil[s]! - now) * 1.2
        if (id === selected) pulse += 0.12
        pulseArr[n] = pulse
        fadeArr[n] = rigged ? 0 : alpha * (1 - 0.15 * ws)

        // tier halo hovering over the head, spinning slowly, dimmed while asleep, gone when dead;
        // on a capability ladder the genius wears a wide bright fast ring and the dunce a small dull slow one
        const prom = slotProminence(id)
        const hy = ground + (HALO_HEIGHT + 0.08 * Math.sin(now * 2.2 + s)) * sy * sc + bob
        dummy.position.set(x, hy, z)
        dummy.rotation.set(0.25 * Math.sin(now * 0.9 + s), now * (0.5 + 0.8 * prom) + s, 0)
        const hs = sc * (0.7 + 0.6 * prom) * (1 - 0.6 * wx) * (1 - 0.3 * ws)
        dummy.scale.set(hs, hs, hs)
        dummy.updateMatrix()
        halos.setMatrixAt(n, dummy.matrix)
        halos.setColorAt(n, color.copy(base).lerp(WHITE, id === selected ? 0.35 : 0.1 + 0.15 * prom))
        hPulse[n] = 0.55 + 0.4 * prom + 0.35 * pulse - 0.5 * ws
        hFade[n] = alpha * (1 - 0.35 * ws) * (1 - wx)

        instToSlot.map[n] = s
        n++
      }
      instToSlot.count = n
      mesh.count = n
      halos.count = n
      blobs.count = nb
      if (n > 0) {
        mesh.instanceMatrix.needsUpdate = true
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
        attrs.pulse.needsUpdate = true
        attrs.fade.needsUpdate = true
        halos.instanceMatrix.needsUpdate = true
        if (halos.instanceColor) halos.instanceColor.needsUpdate = true
        haloAttrs.pulse.needsUpdate = true
        haloAttrs.fade.needsUpdate = true
      }
      if (nb > 0) blobs.instanceMatrix.needsUpdate = true
    }
    return registerSystem(SYS_AGENTS, system)
  }, [mesh, halos, blobs, attrs, haloAttrs, capacity])

  const onMove = (e: ThreeEvent<PointerEvent>) => {
    const i = e.instanceId
    if (i === undefined || i >= instToSlot.count) return
    e.stopPropagation()
    useStore.getState().hover(sampledIdAt(instToSlot.map[i]!))
  }
  const onOut = () => useStore.getState().hover(null)
  const onClick = (e: ThreeEvent<MouseEvent>) => {
    const i = e.instanceId
    if (i === undefined || i >= instToSlot.count) return
    e.stopPropagation()
    useStore.getState().select(sampledIdAt(instToSlot.map[i]!))
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
    <group>
      <primitive ref={meshRef} object={mesh} onPointerMove={onMove} onPointerOut={onOut} onClick={onClick} onDoubleClick={onDouble} />
      <primitive object={halos} />
      <primitive object={blobs} />
    </group>
  )
}

const WHITE = new Color('#ffffff')
const ASH = new Color('#3a3f4c')

function sampledIdAt(slot: number): string | null {
  const id = sampled.out.ids[slot]
  return id ? id : null
}
