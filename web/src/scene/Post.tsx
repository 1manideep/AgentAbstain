/**
 * Post-processing behind the GPU profile: hardware gets N8AO + bloom (entropy
 * pulses and node cores) + vignette with 4× MSAA. A software renderer skips the
 * composer entirely (every pass is a full-screen raster there) and keeps only a
 * CSS vignette over the canvas.
 */
import { Bloom, EffectComposer, N8AO, Vignette } from '@react-three/postprocessing'
import { gpuProfile } from './gpu'

/** Hardware only: a software rasteriser gets a CSS vignette overlay instead (see Scene). */
export function Post() {
  if (gpuProfile.software) return null
  return (
    <EffectComposer multisampling={4} enableNormalPass={false}>
      <N8AO aoRadius={2.2} intensity={1.15} distanceFalloff={0.9} quality="low" halfRes />
      <Bloom mipmapBlur luminanceThreshold={0.9} luminanceSmoothing={0.25} intensity={0.7} radius={0.6} />
      <Vignette eskil={false} offset={0.22} darkness={0.5} />
    </EffectComposer>
  )
}
