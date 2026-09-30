import { useEffect, useMemo } from 'react'
import { Color, FrontSide, PlaneGeometry, ShaderMaterial, UniformsLib, UniformsUtils } from 'three'
import { registerSystem, SYS_ATMOSPHERE, type FrameCtx } from './sceneState'

/**
 * Ground plane centred on the origin (the kernel clamps positions to
 * [−size/2, size/2]) with a 1-unit / 10-unit grid drawn analytically. Line
 * width is derived from view distance instead of screen-space derivatives so
 * the full-screen fragment stays cheap on fill-rate-bound renderers.
 */
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
  uniform float uHalf;
  uniform float uTintAmount;
  uniform float uPixel;
  varying vec3 vWorld;
  void main() {
    vec2 p = vWorld.xz;
    float dist = distance(vWorld, cameraPosition);
    float px = dist * uPixel; // world units per screen pixel at this fragment
    vec3 base = mix(uBase, uTint, uTintAmount);
    // inside the world bounds the grid is visible; outside it fades into the void
    vec2 edge = uHalf - abs(p);
    float inside = smoothstep(-1.0, 1.0, edge.x) * smoothstep(-1.0, 1.0, edge.y);
    // distance to the nearest 1-unit and 10-unit line, no derivatives needed
    vec2 f1 = abs(fract(p) - 0.5);
    float d1 = 0.5 - max(f1.x, f1.y);
    vec2 f10 = abs(fract(p * 0.1) - 0.5) * 10.0;
    float d10 = 5.0 - max(f10.x, f10.y);
    float minor = (1.0 - smoothstep(0.0, px * 1.1, d1)) * 0.16 * clamp(1.0 - px * 1.6, 0.0, 1.0);
    float major = (1.0 - smoothstep(0.0, px * 1.4, d10)) * 0.42 * clamp(1.4 - px * 0.6, 0.0, 1.0);
    float border = (1.0 - smoothstep(0.0, px * 2.0, min(edge.x, edge.y))) * 0.6;
    float lines = max(max(minor, major), border) * inside;
    float vig = 1.0 - smoothstep(0.35, 0.95, length(p) / uHalf * 0.85);
    vec3 col = base * (0.55 + 0.45 * vig) + uLine * lines;
    col = mix(uBase * 0.7, col, max(inside, 0.25));
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
    return new ShaderMaterial({
      vertexShader: vertex,
      fragmentShader: fragment,
      fog: true,
      side: FrontSide,
      uniforms: UniformsUtils.merge([
        UniformsLib.fog,
        {
          uBase: { value: new Color('#101a33') },
          uTint: { value: new Color('#101a33') },
          uLine: { value: new Color('#46588f') },
          uHalf: { value: size / 2 },
          uTintAmount: { value: 0 },
          uPixel: { value: 0.002 },
        },
      ]),
    })
  }, [size])
  const geometry = useMemo(() => new PlaneGeometry(size * 6, size * 6, 1, 1), [size])

  useEffect(() => {
    const system = (ctx: FrameCtx) => {
      // scarcity < 1 → dry/warm tint; > 1 → lush/cool tint; 1 → neutral
      const sc = ctx.out.scarcity
      const amt = Math.min(1, Math.abs(sc - 1) * 1.6)
      const tint = material.uniforms.uTint!.value as Color
      tint.copy(sc < 1 ? DRY : LUSH)
      const cur = material.uniforms.uTintAmount!.value as number
      material.uniforms.uTintAmount!.value = cur + (amt - cur) * Math.min(1, ctx.dt * 2)
      material.uniforms.uPixel!.value = ctx.pixelAngle
    }
    return registerSystem(SYS_ATMOSPHERE, system)
  }, [material])

  useEffect(
    () => () => {
      material.dispose()
      geometry.dispose()
    },
    [material, geometry],
  )

  return <mesh geometry={geometry} material={material} rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.01, 0]} />
}
