/**
 * MeshStandardMaterial extended with two per-instance attributes:
 *   aPulse — emissive strength added on top of the instance colour
 *   aFade  — alpha multiplier (tombstones, births)
 * Injected with onBeforeCompile so lighting, fog and instancing stay stock.
 */
import { DynamicDrawUsage, InstancedBufferAttribute, MeshStandardMaterial, type BufferGeometry } from 'three'

export function makeInstancedMaterial(opts: { roughness?: number; metalness?: number; emissiveScale?: number; rim?: boolean; color?: string } = {}): MeshStandardMaterial {
  const mat = new MeshStandardMaterial({
    roughness: opts.roughness ?? 0.55,
    metalness: opts.metalness ?? 0.08,
    transparent: true,
    depthWrite: true,
    color: opts.color ?? '#ffffff',
  })
  const emissiveScale = (opts.emissiveScale ?? 1.4).toFixed(3)
  const rim = opts.rim ? 1 : 0
  mat.onBeforeCompile = (shader) => {
    shader.vertexShader = shader.vertexShader
      .replace(
        '#include <common>',
        '#include <common>\nattribute float aPulse;\nattribute float aFade;\nvarying float vPulse;\nvarying float vFade;',
      )
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvPulse = aPulse;\nvFade = aFade;')
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying float vPulse;\nvarying float vFade;')
      .replace('#include <color_fragment>', '#include <color_fragment>\ndiffuseColor.a *= vFade;')
      .replace(
        '#include <emissivemap_fragment>',
        rim
          ? `#include <emissivemap_fragment>
{
  // silhouette: darken grazing edges a little and add a faint warm rim so bodies separate from grass, water and sky
  float rimK = pow(1.0 - saturate(dot(normalize(normal), normalize(vViewPosition))), 3.0);
  diffuseColor.rgb *= 1.0 - 0.38 * rimK;
  totalEmissiveRadiance += vec3(0.95, 0.88, 0.78) * rimK * 0.11;
}
totalEmissiveRadiance += diffuseColor.rgb * vPulse * ${emissiveScale};`
          : `#include <emissivemap_fragment>\ntotalEmissiveRadiance += diffuseColor.rgb * vPulse * ${emissiveScale};`,
      )
  }
  mat.customProgramCacheKey = () => 'void-instanced-' + emissiveScale + '-' + rim
  return mat
}

export function attachInstanceAttributes(geometry: BufferGeometry, capacity: number): { pulse: InstancedBufferAttribute; fade: InstancedBufferAttribute } {
  const pulse = new InstancedBufferAttribute(new Float32Array(capacity), 1)
  pulse.setUsage(DynamicDrawUsage)
  const fade = new InstancedBufferAttribute(new Float32Array(capacity).fill(1), 1)
  fade.setUsage(DynamicDrawUsage)
  geometry.setAttribute('aPulse', pulse)
  geometry.setAttribute('aFade', fade)
  return { pulse, fade }
}
