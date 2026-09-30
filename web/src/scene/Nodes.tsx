/**
 * Resource nodes as glowing crystal clusters inside a ring of stones. The
 * crystal core's emissive intensity tracks stock; a forage pulse flashes it.
 * Two draw calls for every node (instanced crystals + instanced stone rings).
 */
import { useEffect, useMemo, useState } from 'react'
import {
  BufferAttribute,
  Color,
  DodecahedronGeometry,
  DynamicDrawUsage,
  InstancedMesh,
  MeshStandardMaterial,
  Object3D,
  OctahedronGeometry,
  type BufferGeometry,
} from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { nonIndexed } from './geom'
import { history } from '../state/history'
import { attachInstanceAttributes, makeInstancedMaterial } from './instancedMaterial'
import { mulberry32 } from './noise'
import { registerSystem, SYS_NODES, type FrameCtx } from './sceneState'

const dummy = new Object3D()
const color = new Color()
const RICH = new Color('#7ff2e6')
const THIN = new Color('#2d6b66')

/** Per-node pulse timers (set by the effects layer on forage / stock_delta). */
export const nodePulse = { until: new Float64Array(64), lastTickB: -1 }

function paint(g: BufferGeometry, hex: string): BufferGeometry {
  const n = g.getAttribute('position').count
  const c = new Color(hex)
  const arr = new Float32Array(n * 3)
  for (let i = 0; i < n; i++) {
    arr[i * 3] = c.r
    arr[i * 3 + 1] = c.g
    arr[i * 3 + 2] = c.b
  }
  g.setAttribute('color', new BufferAttribute(arr, 3))
  return g
}

function crystalGeometry(): BufferGeometry {
  const rand = mulberry32(31337)
  const parts: BufferGeometry[] = []
  const n = 6
  for (let i = 0; i < n; i++) {
    const g = new OctahedronGeometry(0.34 + rand() * 0.2, 0)
    const h = 1.4 + rand() * 1.6
    g.scale(1, h / 0.6, 1)
    const a = (i / n) * Math.PI * 2 + rand() * 0.6
    const r = i === 0 ? 0 : 0.35 + rand() * 0.4
    g.rotateZ((rand() - 0.5) * 0.7)
    g.rotateX((rand() - 0.5) * 0.7)
    g.translate(Math.cos(a) * r, h * 0.45, Math.sin(a) * r)
    paint(g, i % 2 ? '#bff6ff' : '#e6fbff')
    parts.push(nonIndexed(g))
  }
  const merged = mergeGeometries(parts, false)!
  merged.computeVertexNormals()
  merged.computeBoundingSphere()
  return merged
}

function stoneRingGeometry(): BufferGeometry {
  const rand = mulberry32(4242)
  const parts: BufferGeometry[] = []
  const n = 9
  for (let i = 0; i < n; i++) {
    const g = new DodecahedronGeometry(0.32 + rand() * 0.22, 0)
    const a = (i / n) * Math.PI * 2 + rand() * 0.3
    const r = 2.4 + rand() * 0.5
    g.scale(1, 0.75 + rand() * 0.4, 1)
    g.rotateY(rand() * Math.PI)
    g.translate(Math.cos(a) * r, 0.12, Math.sin(a) * r)
    paint(g, i % 3 === 0 ? '#8d919b' : '#6f737d')
    parts.push(nonIndexed(g))
  }
  const merged = mergeGeometries(parts, false)!
  merged.computeVertexNormals()
  merged.computeBoundingSphere()
  return merged
}

export function Nodes() {
  const [capacity, setCapacity] = useState(history.nodeCapacity)
  useEffect(() => history.onCapacityChange((_c, ncap) => setCapacity(ncap)), [])

  const crystalGeom = useMemo(() => crystalGeometry(), [])
  const ringGeom = useMemo(() => stoneRingGeometry(), [])
  const crystalMat = useMemo(() => {
    const m = makeInstancedMaterial({ roughness: 0.25, metalness: 0.1, emissiveScale: 1.6 })
    m.vertexColors = true
    return m
  }, [])
  const ringMat = useMemo(() => new MeshStandardMaterial({ vertexColors: true, roughness: 0.95, metalness: 0 }), [])

  const crystals = useMemo(() => {
    const m = new InstancedMesh(crystalGeom, crystalMat, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    m.setColorAt(0, color.set('#ffffff'))
    m.instanceColor!.setUsage(DynamicDrawUsage)
    return m
  }, [crystalGeom, crystalMat, capacity])
  const rings = useMemo(() => {
    const m = new InstancedMesh(ringGeom, ringMat, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    return m
  }, [ringGeom, ringMat, capacity])
  const attrs = useMemo(() => attachInstanceAttributes(crystalGeom, capacity), [crystalGeom, capacity])
  useEffect(
    () => () => {
      crystals.dispose()
      rings.dispose()
    },
    [crystals, rings],
  )

  useEffect(() => {
    if (nodePulse.until.length < capacity) nodePulse.until = new Float64Array(capacity)
    const system = (ctx: FrameCtx) => {
      const { out, now, reducedMotion } = ctx
      const pulseArr = attrs.pulse.array as Float32Array
      const fadeArr = attrs.fade.array as Float32Array
      if (out.tickB !== nodePulse.lastTickB) {
        nodePulse.lastTickB = out.tickB
        for (let s = 0; s < out.nodeCount; s++) {
          if (out.nodePresent[s] && out.ndelta[s]! < -0.2) nodePulse.until[s] = now + 0.9
        }
      }
      let n = 0
      for (let s = 0; s < out.nodeCount; s++) {
        if (!out.nodePresent[s]) continue
        const cap = out.ncap[s]! > 0 ? out.ncap[s]! : 1
        const fill = Math.min(1, Math.max(0, out.nstock[s]! / cap))
        const x = out.nx[s]!
        const z = out.ny[s]!
        const y = ctx.heightAt(x, z)
        const grow = 0.55 + 0.7 * fill
        dummy.position.set(x, y - 0.05, z)
        dummy.rotation.set(0, now * 0.12 + s, 0)
        dummy.scale.set(0.9 + 0.2 * fill, grow, 0.9 + 0.2 * fill)
        dummy.updateMatrix()
        crystals.setMatrixAt(n, dummy.matrix)
        color.copy(THIN).lerp(RICH, fill)
        crystals.setColorAt(n, color)
        const left = nodePulse.until[s]! - now
        const flash = left > 0 ? left / 0.9 : 0
        const breathe = reducedMotion ? 0 : 0.08 * Math.sin(now * 1.6 + s)
        pulseArr[n] = 0.15 + 1.1 * fill + breathe + flash * 1.3
        fadeArr[n] = 1
        dummy.position.set(x, y - 0.02, z)
        dummy.rotation.set(0, s * 0.7, 0)
        dummy.scale.set(1, 1, 1)
        dummy.updateMatrix()
        rings.setMatrixAt(n, dummy.matrix)
        n++
      }
      crystals.count = n
      rings.count = n
      if (n > 0) {
        crystals.instanceMatrix.needsUpdate = true
        if (crystals.instanceColor) crystals.instanceColor.needsUpdate = true
        attrs.pulse.needsUpdate = true
        attrs.fade.needsUpdate = true
        rings.instanceMatrix.needsUpdate = true
      }
    }
    return registerSystem(SYS_NODES, system)
  }, [crystals, rings, attrs, capacity])

  return (
    <group>
      <primitive object={crystals} />
      <primitive object={rings} />
    </group>
  )
}
