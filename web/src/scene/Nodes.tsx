import { useEffect, useMemo, useState } from 'react'
import { Color, ConeGeometry, DynamicDrawUsage, InstancedMesh, Object3D } from 'three'
import { history } from '../state/history'
import { attachInstanceAttributes, makeInstancedMaterial } from './instancedMaterial'
import { registerSystem, SYS_NODES, type FrameCtx } from './sceneState'

const dummy = new Object3D()
const color = new Color()
const RICH = new Color('#2fbf8f')
const THIN = new Color('#3a5a52')

/** Per-node pulse timers (set by the effects layer on forage / stock_delta). */
export const nodePulse = { until: new Float64Array(64), lastTickB: -1 }

export function Nodes() {
  const [capacity, setCapacity] = useState(history.nodeCapacity)
  useEffect(() => history.onCapacityChange((_c, ncap) => setCapacity(ncap)), [])

  const geometry = useMemo(() => {
    const g = new ConeGeometry(0.85, 1, 7, 1)
    g.translate(0, 0.5, 0)
    return g
  }, [])
  const material = useMemo(() => makeInstancedMaterial({ roughness: 0.7, metalness: 0.05, emissiveScale: 1.2 }), [])
  const mesh = useMemo(() => {
    const m = new InstancedMesh(geometry, material, capacity)
    m.instanceMatrix.setUsage(DynamicDrawUsage)
    m.frustumCulled = false
    m.count = 0
    m.setColorAt(0, color.set('#ffffff'))
    m.instanceColor!.setUsage(DynamicDrawUsage)
    return m
  }, [geometry, material, capacity])
  const attrs = useMemo(() => attachInstanceAttributes(geometry, capacity), [geometry, capacity])
  useEffect(() => () => mesh.dispose(), [mesh])

  useEffect(() => {
    if (nodePulse.until.length < capacity) nodePulse.until = new Float64Array(capacity)
    const system = (ctx: FrameCtx) => {
      const { out, now, reducedMotion } = ctx
      const pulseArr = attrs.pulse.array as Float32Array
      const fadeArr = attrs.fade.array as Float32Array
      // A stock drop at the newer frame fires a pulse when the clock crosses into that segment.
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
        const h = 0.45 + 3.6 * fill
        const r = 0.75 + 0.45 * fill
        dummy.position.set(out.nx[s]!, 0, out.ny[s]!)
        dummy.rotation.set(0, now * 0.15 + s, 0)
        dummy.scale.set(r, h, r)
        dummy.updateMatrix()
        mesh.setMatrixAt(n, dummy.matrix)
        color.copy(THIN).lerp(RICH, fill)
        mesh.setColorAt(n, color)
        const left = nodePulse.until[s]! - now
        const flash = left > 0 ? left / 0.9 : 0
        const idle = reducedMotion ? 0.08 : 0.08 + 0.05 * Math.sin(now * 1.3 + s)
        pulseArr[n] = idle * fill + flash * 0.9
        fadeArr[n] = 1
        n++
      }
      mesh.count = n
      if (n > 0) {
        mesh.instanceMatrix.needsUpdate = true
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
        attrs.pulse.needsUpdate = true
        attrs.fade.needsUpdate = true
      }
    }
    return registerSystem(SYS_NODES, system)
  }, [mesh, attrs, capacity])

  return <primitive object={mesh} />
}
