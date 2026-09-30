import { PerformanceMonitor } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { Suspense, useCallback, useState } from 'react'
import { useStore } from '../state/store'
import { Agents } from './Agents'
import { Atmosphere } from './Atmosphere'
import { CameraRig } from './CameraRig'
import { Driver } from './Driver'
import { Effects } from './Effects'
import { Gadgets } from './Gadgets'
import { Ground } from './Ground'
import { Nodes } from './Nodes'

function initialDpr(): number {
  const d = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
  return Math.min(1.5, Math.max(1, d))
}

export function Scene() {
  const size = useStore((s) => s.config?.world_size ?? 60)
  const runId = useStore((s) => s.runId)
  const [dpr, setDpr] = useState(initialDpr)
  const lower = useCallback(() => setDpr(1), [])
  const raise = useCallback(() => setDpr(initialDpr()), [])
  const onClickMiss = useCallback(() => {
    const st = useStore.getState()
    if (st.followAgentId) st.follow(null)
  }, [])

  return (
    <Canvas
      frameloop="always"
      dpr={dpr}
      gl={{ antialias: true, powerPreference: 'high-performance', alpha: false, stencil: false }}
      camera={{ fov: 42, near: 0.5, far: 600, position: [size * 0.5, size * 0.62, size * 1.22] }}
      onPointerMissed={onClickMiss}
      style={{ position: 'absolute', inset: 0 }}
    >
      <PerformanceMonitor onDecline={lower} onIncline={raise} onFallback={lower} flipflops={3} factor={1}>
        <Driver />
        <Atmosphere />
        <Ground size={size} />
        <Agents />
        <Nodes />
        <Gadgets />
        <Suspense fallback={null}>
          <Effects />
        </Suspense>
        <CameraRig size={size} runId={runId} />
      </PerformanceMonitor>
    </Canvas>
  )
}
