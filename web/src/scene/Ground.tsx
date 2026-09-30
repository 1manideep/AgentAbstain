import { useEffect, useMemo } from 'react'
import { Color, DoubleSide, PlaneGeometry, ShaderMaterial, UniformsLib, UniformsUtils } from 'three'
import { registerSystem, SYS_ATMOSPHERE, type FrameCtx } from './sceneState'

const vertex = /* glsl */ `
  #include <fog_pars_vertex>
  varying vec3 vWorld;
  void main() {
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vWorld = wp.xyz;
    vec4 mvPosition = viewMatrix * wp;
    gl_Position = projectionMatrix * mvPosition;
    #include <fog_vertex>
  }
`

const fragment = /* glsl */ `
  #include <fog_pars_fragment>
  uniform vec3 uBase;
  uniform vec3 uTint;
  uniform vec3 uLine;
  uniform float uSize;
  uniform float uTintAmount;
  varying vec3 vWorld;
  float gridLine(vec2 p, float period, float width) {
    vec2 g = abs(fract(p / period - 0.5) - 0.5) * period / fwidth(p);
    float d = min(g.x, g.y);
    return 1.0 - smoothstep(width, width + 1.0, d);
  }
  void main() {
    vec2 p = vWorld.xz;
    vec3 base = mix(uBase, uTint, uTintAmount);
    // inside the world bounds the grid is visible; outside it fades to the void
    float inside = 1.0;
    inside *= smoothstep(-1.0, 1.0, p.x) * smoothstep(-1.0, 1.0, uSize - p.x);
    inside *= smoothstep(-1.0, 1.0, p.y) * smoothstep(-1.0, 1.0, uSize - p.y);
    float minor = gridLine(p, 1.0, 0.6) * 0.16;
    float major = gridLine(p, 10.0, 0.9) * 0.42;
    float edge = gridLine(p - vec2(0.0), uSize, 1.4) * 0.6;
    float lines = max(max(minor, major), edge) * inside;
    // gentle radial vignette so the centre reads brighter
    vec2 c = p / uSize - 0.5;
    float vig = 1.0 - smoothstep(0.35, 0.95, length(c) * 1.2);
    vec3 col = base * (0.55 + 0.45 * vig) + uLine * lines;
    col = mix(uBase * 0.55, col, max(inside, 0.15));
    gl_FragColor = vec4(col, 1.0);
    #include <fog_fragment>
  }
`

interface GroundProps {
  size: number
}

const DRY = new Color('#3a2c1f')
const LUSH = new Color('#0f3b3a')

export function Ground({ size }: GroundProps) {
  const material = useMemo(() => {
    const m = new ShaderMaterial({
      vertexShader: vertex,
      fragmentShader: fragment,
      fog: true,
      side: DoubleSide,
      uniforms: UniformsUtils.merge([
        UniformsLib.fog,
        {
          uBase: { value: new Color('#0c1224') },
          uTint: { value: new Color('#0c1224') },
          uLine: { value: new Color('#3b4a7a') },
          uSize: { value: size },
          uTintAmount: { value: 0 },
        },
      ]),
    })
    return m
  }, [size])
  const geometry = useMemo(() => new PlaneGeometry(size + 40, size + 40, 1, 1), [size])

  useEffect(() => {
    const system = (ctx: FrameCtx) => {
      // scarcity < 1 → dry/warm tint; > 1 → lush/cool tint; 1 → neutral
      const sc = ctx.out.scarcity
      const amt = Math.min(1, Math.abs(sc - 1) * 1.6)
      const tint = material.uniforms.uTint!.value as Color
      tint.copy(sc < 1 ? DRY : LUSH)
      material.uniforms.uTintAmount!.value += (amt - (material.uniforms.uTintAmount!.value as number)) * Math.min(1, ctx.dt * 2)
    }
    return registerSystem(SYS_ATMOSPHERE, system)
  }, [material])

  useEffect(() => () => {
    material.dispose()
    geometry.dispose()
  }, [material, geometry])

  return (
    <mesh geometry={geometry} material={material} rotation={[-Math.PI / 2, 0, 0]} position={[size / 2, -0.01, size / 2]} />
  )
}
