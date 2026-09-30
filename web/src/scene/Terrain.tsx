/**
 * Terrain mesh (one BufferGeometry, vertex colours by height and slope) and
 * the water plane. The Terrain object is built once the first snapshot reveals
 * the resource nodes (basins and paths depend on them) and rebuilt only when
 * the node set changes; every other system samples `terrainState.terrain`.
 */
import { useEffect, useMemo, useState } from 'react'
import { BackSide, Color, DoubleSide, MeshLambertMaterial, MeshStandardMaterial, PlaneGeometry, ShaderMaterial, UniformsLib, UniformsUtils, Vector3 } from 'three'
import { gpuProfile } from './gpu'
import { mirror, registerSystem, SYS_TERRAIN, type FrameCtx } from './sceneState'
import { skyState } from './skyState'
import { setTerrain, Terrain, terrainState, WATER_LEVEL } from './terrain'

const TERRAIN_RES = gpuProfile.software ? 84 : 128

type TerrainMaterial = MeshStandardMaterial | MeshLambertMaterial

function makeTerrainMaterial(): TerrainMaterial {
  // Lambert is a fraction of the per-fragment cost of the PBR shader: what a software rasteriser needs
  const mat: TerrainMaterial = gpuProfile.software
    ? new MeshLambertMaterial({ vertexColors: true })
    : new MeshStandardMaterial({ vertexColors: true, roughness: 0.96, metalness: 0.0 })
  const uniforms = {
    uStraw: { value: 0 },
    uDark: { value: 0 },
    uGrid: { value: 0 },
    uHalf: { value: 30 },
    uPixel: { value: 0.002 },
    uCamPos: { value: new Vector3() },
  }
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms)
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nattribute float aGrass;\nvarying float vGrass;\nvarying vec3 vWorldPos;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvGrass = aGrass;\nvWorldPos = (modelMatrix * vec4(transformed, 1.0)).xyz;')
    shader.fragmentShader = shader.fragmentShader
      .replace(
        '#include <common>',
        `#include <common>
varying float vGrass;
varying vec3 vWorldPos;
uniform float uStraw;
uniform float uDark;
uniform float uGrid;
uniform float uHalf;
uniform float uPixel;
uniform vec3 uCamPos;`,
      )
      .replace(
        '#include <color_fragment>',
        `#include <color_fragment>
// scarcity turns the grass to straw; storms darken everything
diffuseColor.rgb = mix(diffuseColor.rgb, diffuseColor.rgb * vec3(1.3, 1.05, 0.5), uStraw * vGrass);
diffuseColor.rgb *= 1.0 - 0.32 * uDark;
if (uGrid > 0.0) {
  vec2 p = vWorldPos.xz;
  float px = distance(vWorldPos, uCamPos) * uPixel;
  vec2 f1 = abs(fract(p) - 0.5);
  float d1 = 0.5 - max(f1.x, f1.y);
  vec2 f10 = abs(fract(p * 0.1) - 0.5) * 10.0;
  float d10 = 5.0 - max(f10.x, f10.y);
  float minor = (1.0 - smoothstep(0.0, px * 1.2, d1)) * 0.22 * clamp(1.0 - px * 1.6, 0.0, 1.0);
  float major = (1.0 - smoothstep(0.0, px * 1.6, d10)) * 0.5 * clamp(1.4 - px * 0.6, 0.0, 1.0);
  vec2 edge = uHalf - abs(p);
  float border = (1.0 - smoothstep(0.0, px * 2.4, min(edge.x, edge.y))) * 0.8;
  float inside = step(0.0, edge.x) * step(0.0, edge.y);
  float lines = max(max(minor, major) * inside, border);
  diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.62, 0.72, 1.0), lines * uGrid);
}`,
      )
  }
  mat.customProgramCacheKey = () => 'void-terrain'
  ;(mat as TerrainMaterial & { voidUniforms: typeof uniforms }).voidUniforms = uniforms
  return mat
}

const waterVertex = /* glsl */ `
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
const waterFragment = /* glsl */ `
  #include <fog_pars_fragment>
  uniform float uTime;
  uniform vec3 uSunDir;
  uniform vec3 uSunColor;
  uniform vec3 uDeep;
  uniform vec3 uShallow;
  uniform float uDaylight;
  uniform float uExtent;
  varying vec3 vWorld;
  void main() {
    vec2 p = vWorld.xz;
    // cheap animated normal perturbation: three travelling waves
    float t = uTime;
    float nx = sin(p.x * 0.9 + t * 0.9) * 0.06 + sin((p.x + p.y) * 0.45 - t * 0.6) * 0.05 + sin(p.y * 1.7 + t * 1.3) * 0.02;
    float nz = cos(p.y * 0.8 - t * 0.8) * 0.06 + cos((p.x - p.y) * 0.5 + t * 0.5) * 0.05 + cos(p.x * 1.9 - t * 1.1) * 0.02;
    vec3 n = normalize(vec3(nx, 1.0, nz));
    vec3 v = normalize(cameraPosition - vWorld);
    float fres = pow(1.0 - max(dot(n, v), 0.0), 3.0);
    vec3 col = mix(uDeep, uShallow, 0.35 + 0.65 * fres) * (0.25 + 0.75 * uDaylight);
    vec3 h = normalize(uSunDir + v);
    float spec = pow(max(dot(n, h), 0.0), 90.0) * max(uSunDir.y, 0.0);
    col += uSunColor * spec * 0.9;
    float edge = 1.0 - smoothstep(uExtent * 0.72, uExtent * 0.98, max(abs(p.x), abs(p.y)));
    gl_FragColor = vec4(col, (0.72 + 0.2 * fres) * edge);
    #include <fog_fragment>
  }
`

function nodesKey(ctx: FrameCtx): string {
  const out = ctx.out
  let k = ''
  for (let s = 0; s < out.nodeCount; s++) if (out.nodePresent[s]) k += out.nodeIds[s] + ':' + out.nx[s]!.toFixed(1) + ',' + out.ny[s]!.toFixed(1) + ';'
  return k
}

export function TerrainMesh() {
  const [rev, setRev] = useState(terrainState.rev)
  useEffect(() => {
    const fn = () => setRev(terrainState.rev)
    terrainState.listeners.add(fn)
    return () => {
      terrainState.listeners.delete(fn)
    }
  }, [])

  const material = useMemo(() => makeTerrainMaterial(), [])
  const uniforms = (material as TerrainMaterial & { voidUniforms: { uStraw: { value: number }; uDark: { value: number }; uGrid: { value: number }; uHalf: { value: number }; uPixel: { value: number }; uCamPos: { value: Vector3 } } }).voidUniforms

  const geometry = useMemo(() => {
    void rev
    const t = terrainState.terrain
    return t ? t.buildGeometry(TERRAIN_RES) : null
  }, [rev])
  useEffect(() => () => geometry?.dispose(), [geometry])

  const water = useMemo(() => {
    const mat = new ShaderMaterial({
      vertexShader: waterVertex,
      fragmentShader: waterFragment,
      transparent: true,
      depthWrite: false,
      fog: true,
      side: DoubleSide,
      uniforms: UniformsUtils.merge([
        UniformsLib.fog,
        {
          uTime: { value: 0 },
          uSunDir: { value: new Vector3(0, 1, 0) },
          uSunColor: { value: new Color('#ffe9c4') },
          uDeep: { value: new Color('#0e2f4a') },
          uShallow: { value: new Color('#2b6f8f') },
          uDaylight: { value: 1 },
          uExtent: { value: 66 },
        },
      ]),
    })
    return mat
  }, [])
  const waterGeom = useMemo(() => new PlaneGeometry(2, 2, 1, 1), [])
  useEffect(
    () => () => {
      water.dispose()
      waterGeom.dispose()
      material.dispose()
    },
    [water, waterGeom, material],
  )

  useEffect(() => {
    let lastKey = ''
    let building = false
    let straw = 0
    let dark = 0
    let grid = 0
    const system = (ctx: FrameCtx) => {
      // (Re)build the terrain when the node set is first known or changes; off the frame loop.
      if (!building) {
        const key = nodesKey(ctx)
        if (key !== '' && key !== lastKey) {
          lastKey = key
          building = true
          const out = ctx.out
          const nodes: Array<{ id: string; x: number; y: number }> = []
          for (let s = 0; s < out.nodeCount; s++) if (out.nodePresent[s]) nodes.push({ id: out.nodeIds[s]!, x: out.nx[s]!, y: out.ny[s]! })
          const seed = mirror.seed
          const size = mirror.worldSize
          setTimeout(() => {
            setTerrain(new Terrain(seed, size, nodes))
            building = false
          }, 0)
        }
      }
      const sc = ctx.out.scarcity
      const strawTarget = Math.min(1, Math.max(0, (1 - sc) * 1.8))
      straw += (strawTarget - straw) * Math.min(1, ctx.dt * 1.5)
      const storm = Math.max(0, -ctx.out.weather)
      dark += (storm - dark) * Math.min(1, ctx.dt * 1.5)
      grid += ((mirror.showGrid ? 1 : 0) - grid) * Math.min(1, ctx.dt * 6)
      uniforms.uStraw.value = straw
      uniforms.uDark.value = dark
      uniforms.uGrid.value = grid < 0.01 ? 0 : grid
      uniforms.uHalf.value = mirror.worldSize / 2
      uniforms.uPixel.value = ctx.pixelAngle
      if (ctx.camera) uniforms.uCamPos.value.copy(ctx.camera.position)
      const wu = water.uniforms
      wu.uTime!.value = ctx.now
      ;(wu.uSunDir!.value as Vector3).copy(skyState.sunDir)
      ;(wu.uSunColor!.value as Color).copy(skyState.sunColor)
      wu.uDaylight!.value = skyState.daylight
      const t = terrainState.terrain
      wu.uExtent!.value = t ? t.extent : 66
    }
    return registerSystem(SYS_TERRAIN, system)
  }, [uniforms, water])

  const extent = terrainState.terrain?.extent ?? 66
  return (
    <group>
      {geometry ? <mesh geometry={geometry} material={material} receiveShadow={false} /> : null}
      <mesh geometry={waterGeom} material={water} rotation={[-Math.PI / 2, 0, 0]} position={[0, WATER_LEVEL, 0]} scale={[extent, extent, 1]} renderOrder={1} />
      {/* a dark underside so the void beneath the terrain never shows at grazing angles */}
      <mesh position={[0, -3, 0]} rotation={[-Math.PI / 2, 0, 0]} scale={[extent * 1.2, extent * 1.2, 1]}>
        <planeGeometry args={[2, 2]} />
        <meshBasicMaterial color="#05070d" side={BackSide} />
      </mesh>
    </group>
  )
}
