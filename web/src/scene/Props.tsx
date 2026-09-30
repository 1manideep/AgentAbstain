/**
 * Instanced low-poly props placed by the terrain's seeded scatter: three tree
 * species, rocks and grass tufts. Five draw calls in total; transforms are set
 * once per terrain build. A GLB pack (useGLTFPack) replaces the procedural
 * geometry/material per species when present.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  BufferAttribute,
  Color,
  ConeGeometry,
  CylinderGeometry,
  DoubleSide,
  DodecahedronGeometry,
  IcosahedronGeometry,
  InstancedMesh,
  MeshLambertMaterial,
  MeshStandardMaterial,
  Object3D,
  PlaneGeometry,
  type BufferGeometry,
  type Material,
} from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { nonIndexed } from './geom'
import { useGLTFPack } from './gltfPack'
import { gpuProfile } from './gpu'
import { mulberry32 } from './noise'
import { terrainState, type Terrain } from './terrain'

const dummy = new Object3D()
const color = new Color()

function paint(g: BufferGeometry, hex: string, shade = 0): BufferGeometry {
  const n = g.getAttribute('position').count
  const c = new Color(hex)
  const arr = new Float32Array(n * 3)
  const pos = g.getAttribute('position')
  for (let i = 0; i < n; i++) {
    const k = 1 - shade * Math.max(0, 1 - pos.getY(i) / 2)
    arr[i * 3] = c.r * k
    arr[i * 3 + 1] = c.g * k
    arr[i * 3 + 2] = c.b * k
  }
  g.setAttribute('color', new BufferAttribute(arr, 3))
  return g
}

function merge(parts: BufferGeometry[]): BufferGeometry {
  const clean = parts.map((p) => nonIndexed(p))
  const merged = mergeGeometries(clean, false)
  if (!merged) throw new Error('mergeGeometries failed')
  merged.computeVertexNormals()
  merged.computeBoundingSphere()
  for (const p of clean) p.dispose()
  return merged
}

function pineGeometry(): BufferGeometry {
  const trunk = paint(new CylinderGeometry(0.12, 0.18, 0.9, 6), '#5b3d24')
  trunk.translate(0, 0.45, 0)
  const c1 = paint(new ConeGeometry(0.95, 1.5, 7), '#2f6b35', 0.35)
  c1.translate(0, 1.35, 0)
  const c2 = paint(new ConeGeometry(0.72, 1.3, 7), '#3a7d3c', 0.35)
  c2.translate(0, 2.15, 0)
  const c3 = paint(new ConeGeometry(0.45, 1.0, 7), '#4a8f45', 0.35)
  c3.translate(0, 2.9, 0)
  return merge([trunk, c1, c2, c3])
}

function roundTreeGeometry(): BufferGeometry {
  const trunk = paint(new CylinderGeometry(0.14, 0.2, 1.2, 6), '#6a4a2c')
  trunk.translate(0, 0.6, 0)
  const c1 = paint(new IcosahedronGeometry(0.95, 1), '#4f8f3d', 0.4)
  c1.translate(0, 1.85, 0)
  const c2 = paint(new IcosahedronGeometry(0.6, 1), '#5fa04a', 0.4)
  c2.translate(0.45, 2.35, 0.2)
  return merge([trunk, c1, c2])
}

function bushGeometry(): BufferGeometry {
  const c1 = paint(new IcosahedronGeometry(0.62, 1), '#57944a', 0.5)
  c1.translate(0, 0.5, 0)
  const c2 = paint(new IcosahedronGeometry(0.45, 1), '#6aa653', 0.5)
  c2.translate(0.4, 0.4, 0.25)
  const c3 = paint(new IcosahedronGeometry(0.4, 1), '#4c8a42', 0.5)
  c3.translate(-0.35, 0.35, -0.2)
  return merge([c1, c2, c3])
}

function rockGeometry(seed: number): BufferGeometry {
  const g = new DodecahedronGeometry(0.7, 0)
  const rand = mulberry32(seed)
  const pos = g.getAttribute('position') as BufferAttribute
  // deform vertices deterministically (shared vertices keep the same offset via position hashing)
  const cache = new Map<string, number>()
  for (let i = 0; i < pos.count; i++) {
    const key = `${pos.getX(i).toFixed(3)},${pos.getY(i).toFixed(3)},${pos.getZ(i).toFixed(3)}`
    let k = cache.get(key)
    if (k === undefined) {
      k = 0.75 + rand() * 0.5
      cache.set(key, k)
    }
    pos.setXYZ(i, pos.getX(i) * k, pos.getY(i) * k * 0.7, pos.getZ(i) * k)
  }
  g.translate(0, 0.3, 0)
  paint(g, '#7c7f88', 0.3)
  g.computeVertexNormals()
  return g
}

function grassGeometry(): BufferGeometry {
  const a = new PlaneGeometry(0.8, 0.55, 1, 1)
  a.translate(0, 0.27, 0)
  const b = a.clone()
  b.rotateY(Math.PI / 2)
  const c = a.clone()
  c.rotateY(Math.PI / 4)
  const g = merge([a, b, c])
  // gradient: darker at the root
  const n = g.getAttribute('position').count
  const arr = new Float32Array(n * 3)
  const pos = g.getAttribute('position')
  const lo = new Color('#3d7a34')
  const hi = new Color('#8bc46a')
  for (let i = 0; i < n; i++) {
    const t = Math.min(1, Math.max(0, pos.getY(i) / 0.55))
    color.copy(lo).lerp(hi, t)
    arr[i * 3] = color.r
    arr[i * 3 + 1] = color.g
    arr[i * 3 + 2] = color.b
  }
  g.setAttribute('color', new BufferAttribute(arr, 3))
  return g
}

interface Species {
  key: string
  geometry: BufferGeometry
  material: Material
  spacing: number
  salt: number
  maxSlope: number
  scale: [number, number]
  limit: number
  tint: [string, string]
}

function useSpecies(): Species[] {
  return useMemo(() => {
    // a software rasteriser is vertex- and fill-bound: Lambert shading and sparser placement
    const sw = gpuProfile.software
    const solid = sw ? new MeshLambertMaterial({ vertexColors: true }) : new MeshStandardMaterial({ vertexColors: true, roughness: 0.9, metalness: 0 })
    const leaf = sw
      ? new MeshLambertMaterial({ vertexColors: true, side: DoubleSide })
      : new MeshStandardMaterial({ vertexColors: true, roughness: 0.95, metalness: 0, side: DoubleSide })
    const d = sw ? 1.35 : 1
    const l = sw ? 0.55 : 1
    return [
      { key: 'tree_pine', geometry: pineGeometry(), material: solid, spacing: 7.5 * d, salt: 11, maxSlope: 0.5, scale: [0.85, 1.35], limit: Math.round(80 * l), tint: ['#dfe8d6', '#ffffff'] },
      { key: 'tree_round', geometry: roundTreeGeometry(), material: solid, spacing: 9 * d, salt: 12, maxSlope: 0.45, scale: [0.8, 1.25], limit: Math.round(60 * l), tint: ['#e6ead0', '#ffffff'] },
      { key: 'tree_bush', geometry: bushGeometry(), material: solid, spacing: 6 * d, salt: 13, maxSlope: 0.6, scale: [0.7, 1.2], limit: Math.round(90 * l), tint: ['#d8e6c8', '#ffffff'] },
      { key: 'rock', geometry: rockGeometry(5), material: solid, spacing: 8 * d, salt: 14, maxSlope: 1.0, scale: [0.5, 1.6], limit: Math.round(70 * l), tint: ['#c9ccd4', '#ffffff'] },
      { key: 'grass', geometry: grassGeometry(), material: leaf, spacing: 2.2 * d, salt: 15, maxSlope: 0.5, scale: [0.7, 1.4], limit: Math.round(700 * l), tint: ['#e8f0d8', '#ffffff'] },
    ]
  }, [])
}

function SpeciesMesh({ sp, terrain, pack }: { sp: Species; terrain: Terrain; pack: ReturnType<typeof useGLTFPack> }) {
  const entry = pack?.[sp.key]
  const geometry = entry?.geometry ?? sp.geometry
  const material = entry?.material ?? sp.material
  const mesh = useMemo(() => {
    const spots = terrain.scatter({ spacing: sp.spacing, salt: sp.salt, maxSlope: sp.maxSlope, limit: sp.limit, minHeight: sp.key === 'grass' ? 0.12 : 0.2 })
    const m = new InstancedMesh(geometry, material, Math.max(1, spots.length))
    m.frustumCulled = false
    const a = new Color(sp.tint[0])
    const b = new Color(sp.tint[1])
    for (let i = 0; i < spots.length; i++) {
      const p = spots[i]!
      const sc = sp.scale[0] + (sp.scale[1] - sp.scale[0]) * p.r
      dummy.position.set(p.x, p.y - 0.04, p.z)
      dummy.rotation.set(0, p.r * Math.PI * 2, 0)
      dummy.scale.set(sc, sc * (sp.key === 'grass' ? 0.9 + p.r * 0.4 : 1), sc)
      dummy.updateMatrix()
      m.setMatrixAt(i, dummy.matrix)
      m.setColorAt(i, color.copy(a).lerp(b, (p.r * 7.31) % 1))
    }
    m.count = spots.length
    m.instanceMatrix.needsUpdate = true
    if (m.instanceColor) m.instanceColor.needsUpdate = true
    return m
  }, [sp, terrain, geometry, material])
  useEffect(() => () => mesh.dispose(), [mesh])
  return <primitive object={mesh} />
}

export function Props() {
  const [rev, setRev] = useState(terrainState.rev)
  useEffect(() => {
    const fn = () => setRev(terrainState.rev)
    terrainState.listeners.add(fn)
    return () => {
      terrainState.listeners.delete(fn)
    }
  }, [])
  const species = useSpecies()
  const pack = useGLTFPack()
  const terrain = terrainState.terrain
  void rev
  if (!terrain) return null
  return (
    <group>
      {species.map((sp) => (
        <SpeciesMesh key={sp.key + rev} sp={sp} terrain={terrain} pack={pack} />
      ))}
    </group>
  )
}
