import { useEffect, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import { Color, DirectionalLight, FogExp2, HemisphereLight } from 'three'
import { registerSystem, SYS_ATMOSPHERE, type FrameCtx } from './sceneState'

const BG_CLEAR = new Color('#0d1530')
const BG_STORM = new Color('#05070e')
const SUN_CLEAR = new Color('#fff1dc')
const SUN_STORM = new Color('#8d9bc4')

/** Weather → fog density and sky tint; lights dim under storms. */
export function Atmosphere() {
  const scene = useThree((s) => s.scene)
  const fog = useMemo(() => new FogExp2(BG_CLEAR.clone(), 0.0036), [])
  const bg = useMemo(() => BG_CLEAR.clone(), [])
  const sun = useRef<DirectionalLight>(null)
  const hemi = useRef<HemisphereLight>(null)

  useEffect(() => {
    scene.fog = fog
    scene.background = bg
    return () => {
      if (scene.fog === fog) scene.fog = null
      if (scene.background === bg) scene.background = null
    }
  }, [scene, fog, bg])

  useEffect(() => {
    let weather = 0
    const system = (ctx: FrameCtx) => {
      // ease the sampled weather so tick-to-tick changes never pop
      weather += (ctx.out.weather - weather) * Math.min(1, ctx.dt * 1.5)
      const storm = Math.max(0, -weather)
      const clear = Math.max(0, weather)
      bg.copy(BG_CLEAR).lerp(BG_STORM, storm)
      bg.r += 0.02 * clear
      bg.g += 0.02 * clear
      fog.color.copy(bg)
      fog.density = 0.0036 + 0.016 * storm
      if (sun.current) {
        sun.current.intensity = 1.25 - 0.8 * storm + 0.2 * clear
        sun.current.color.copy(SUN_CLEAR).lerp(SUN_STORM, storm)
      }
      if (hemi.current) hemi.current.intensity = 0.95 - 0.35 * storm
    }
    return registerSystem(SYS_ATMOSPHERE, system)
  }, [bg, fog])

  return (
    <>
      <hemisphereLight ref={hemi} args={['#7b8fc9', '#0b0e18', 0.95]} />
      <directionalLight ref={sun} position={[18, 42, 12]} intensity={1.25} color={SUN_CLEAR} />
    </>
  )
}
