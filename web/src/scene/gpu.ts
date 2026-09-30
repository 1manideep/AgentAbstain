/**
 * One-time probe of the WebGL renderer so the scene can pick a render profile
 * before the Canvas exists. A software rasteriser (SwiftShader, llvmpipe, Mesa
 * offscreen) is fill-rate bound, so the scene drops MSAA and allows a lower
 * pixel ratio there; on a real GPU the defaults from DESIGN §16 apply.
 */
export interface GpuProfile {
  renderer: string
  software: boolean
  antialias: boolean
  /** Ordered from lowest to highest quality; PerformanceMonitor moves along it. */
  dprSteps: number[]
}

function probe(): GpuProfile {
  const deviceDpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
  const hi = Math.min(1.5, Math.max(1, deviceDpr))
  let renderer = 'unknown'
  try {
    const c = document.createElement('canvas')
    const gl = (c.getContext('webgl2') ?? c.getContext('webgl')) as WebGLRenderingContext | null
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info')
      renderer = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER))
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  } catch {
    /* no WebGL: the Canvas will surface its own error */
  }
  const software = /swiftshader|llvmpipe|softpipe|software|mesa offscreen|microsoft basic render/i.test(renderer)
  return {
    renderer,
    software,
    antialias: !software,
    dprSteps: software ? [0.75, 1] : hi > 1 ? [1, hi] : [1],
  }
}

export const gpuProfile: GpuProfile = probe()
