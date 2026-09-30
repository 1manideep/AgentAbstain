import { PerformanceMonitor } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { Suspense, useCallback, useEffect, useState } from 'react'
import { useStore } from '../state/store'
import { Agents } from './Agents'
import { Atmosphere } from './Atmosphere'
import { CameraRig } from './CameraRig'
import { Driver } from './Driver'
import { Effects } from './Effects'
import { Gadgets } from './Gadgets'
import { gpuProfile } from './gpu'
import { Ground } from './Ground'
import { Nodes } from './Nodes'

const steps = gpuProfile.dprSteps

export function Scene() {
  const size = useStore((s) => s.config?.world_size ?? 60)
  const runId = useStore((s) => s.runId)
  // Start at the top quality step; PerformanceMonitor lowers dpr first (§16).
  const [step, setStep] = useState(steps.length - 1)
  const dpr = steps[step]!
  const lower = useCallback(() => setStep((s) => Math.max(0, s - 1)), [])
  const raise = useCallback(() => setStep((s) => Math.min(steps.length - 1, s + 1)), [])
  const onClickMiss = useCallback(() => {
    const st = useStore.getState()
    if (st.followAgentId) st.follow(null)
  }, [])
  useEffect(() => {
    if (gpuProfile.software) console.info(`[void] software renderer detected (${gpuProfile.renderer}): MSAA off, dpr ${steps.join('/')}`)
  }, [])

  return (
    <Canvas
      frameloop="always"
      dpr={dpr}
      gl={{ antialias: gpuProfile.antialias, powerPreference: 'high-performance', alpha: false, stencil: false }}
      camera={{ fov: 42, near: 0.5, far: 600, position: [0, size * 0.5, size * 0.85] }}
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
