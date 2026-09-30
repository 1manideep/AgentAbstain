import { PerformanceMonitor } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { Suspense, useCallback, useEffect, useState } from 'react'
import { useStore } from '../state/store'
import { Agents } from './Agents'
import { CameraRig } from './CameraRig'
import { Driver } from './Driver'
import { Effects } from './Effects'
import { Gadgets } from './Gadgets'
import { gpuProfile } from './gpu'
import { Nodes } from './Nodes'
import { Post } from './Post'
import { Props } from './Props'
import { Rig } from './Rig'
import { Sky } from './Sky'
import { TerrainMesh } from './Terrain'

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
    if (gpuProfile.software) console.info(`[void] software renderer detected (${gpuProfile.renderer}): MSAA off, bloom/AO off, dpr ${steps.join('/')}`)
  }, [])

  return (
    <>
    <Canvas
      frameloop="always"
      dpr={dpr}
      gl={{ antialias: gpuProfile.antialias, powerPreference: 'high-performance', alpha: false, stencil: false }}
      camera={{ fov: 42, near: 0.5, far: 900, position: [0, size * 0.5, size * 0.85] }}
      onPointerMissed={onClickMiss}
      style={{ position: 'absolute', inset: 0 }}
    >
      <PerformanceMonitor onDecline={lower} onIncline={raise} onFallback={lower} flipflops={3} factor={1}>
        <Driver />
        <Sky />
        <TerrainMesh />
        <Props />
        <Agents />
        <Rig />
        <Nodes />
        <Gadgets />
        <Suspense fallback={null}>
          <Effects />
        </Suspense>
        <CameraRig size={size} runId={runId} />
        <Post />
      </PerformanceMonitor>
    </Canvas>
    {gpuProfile.software ? <div className="vignette" aria-hidden="true" /> : null}
    </>
  )
}
