import { OrbitControls } from '@react-three/drei'
import { useThree } from '@react-three/fiber'
import { useCallback, useEffect, useRef } from 'react'
import { Vector3 } from 'three'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { cameraCommands, mirror, registerSystem, SYS_CAMERA, type FrameCtx } from './sceneState'

interface CameraRigProps {
  size: number
  runId: string | null
}

interface SavedCamera {
  p: [number, number, number]
  t: [number, number, number]
}

const tmp = new Vector3()
const tmp2 = new Vector3()

function storageKey(runId: string | null): string {
  return `void.cam.${runId ?? 'none'}`
}

export function CameraRig({ size, runId }: CameraRigProps) {
  const controls = useRef<OrbitControlsImpl>(null)
  const camera = useThree((s) => s.camera)
  const anim = useRef<{ pos: Vector3; target: Vector3 } | null>(null)

  // Restore per-run camera state, else frame the world.
  useEffect(() => {
    const c = controls.current
    if (!c) return
    let saved: SavedCamera | null = null
    try {
      const raw = localStorage.getItem(storageKey(runId))
      if (raw) saved = JSON.parse(raw) as SavedCamera
    } catch {
      saved = null
    }
    if (saved && Array.isArray(saved.p) && Array.isArray(saved.t)) {
      camera.position.set(saved.p[0], saved.p[1], saved.p[2])
      c.target.set(saved.t[0], saved.t[1], saved.t[2])
    } else {
      camera.position.set(0, size * 0.5, size * 0.85)
      c.target.set(0, 0, 0)
    }
    c.update()
  }, [runId, size, camera])

  const persist = useCallback(() => {
    const c = controls.current
    if (!c) return
    const data: SavedCamera = {
      p: [camera.position.x, camera.position.y, camera.position.z],
      t: [c.target.x, c.target.y, c.target.z],
    }
    try {
      localStorage.setItem(storageKey(runId), JSON.stringify(data))
    } catch {
      /* storage unavailable */
    }
  }, [camera, runId])

  const onStart = useCallback(() => {
    // user drag cancels follow and any framing animation
    anim.current = null
    if (useStore.getState().followAgentId) useStore.getState().follow(null)
  }, [])

  useEffect(() => {
    const system = (ctx: FrameCtx) => {
      const c = controls.current
      if (!c) return
      const { out, dt } = ctx
      if (cameraCommands.frameRequested) {
        cameraCommands.frameRequested = false
        let minX = Infinity
        let maxX = -Infinity
        let minZ = Infinity
        let maxZ = -Infinity
        let n = 0
        for (let s = 0; s < out.count; s++) {
          if (!out.present[s] || out.dead[s]) continue
          const x = out.x[s]!
          const z = out.y[s]!
          if (x < minX) minX = x
          if (x > maxX) maxX = x
          if (z < minZ) minZ = z
          if (z > maxZ) maxZ = z
          n++
        }
        if (n === 0) {
          minX = -size / 2
          maxX = size / 2
          minZ = -size / 2
          maxZ = size / 2
        }
        const cx = (minX + maxX) / 2
        const cz = (minZ + maxZ) / 2
        const extent = Math.max(maxX - minX, maxZ - minZ, 8) + 6
        const dist = Math.min(size * 1.5, Math.max(6, extent * 1.05))
        // keep the current view direction, move to a distance that fits the population
        tmp.copy(camera.position).sub(c.target)
        if (tmp.lengthSq() < 1e-6) tmp.set(0, 0.7, 1)
        tmp.normalize()
        if (tmp.y < 0.35) tmp.y = 0.35
        tmp.normalize().multiplyScalar(dist)
        anim.current = { target: new Vector3(cx, 0, cz), pos: new Vector3(cx, 0, cz).add(tmp) }
        useStore.getState().follow(null)
      }
      const k = 1 - Math.exp(-6 * dt)
      const a = anim.current
      if (a) {
        c.target.lerp(a.target, k)
        camera.position.lerp(a.pos, k)
        if (c.target.distanceToSquared(a.target) < 1e-4 && camera.position.distanceToSquared(a.pos) < 1e-3) anim.current = null
        c.update()
        return
      }
      const followId = mirror.followId
      if (followId) {
        const s = history.agentIndex.get(followId)
        if (s !== undefined && s < out.count && out.present[s]) {
          tmp.set(out.x[s]!, 0.9, out.y[s]!)
          tmp2.copy(tmp).sub(c.target).multiplyScalar(k)
          c.target.add(tmp2)
          camera.position.add(tmp2)
          c.update()
        }
      }
    }
    return registerSystem(SYS_CAMERA, system)
  }, [camera, size])

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enableDamping
      dampingFactor={0.09}
      rotateSpeed={0.6}
      panSpeed={0.7}
      zoomSpeed={0.8}
      minDistance={6}
      maxDistance={1.5 * size}
      minPolarAngle={0.15}
      maxPolarAngle={1.45}
      onStart={onStart}
      onEnd={persist}
    />
  )
}

