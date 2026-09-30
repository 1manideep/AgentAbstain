import { memo, useEffect, useMemo } from 'react'
import {
  BoxGeometry,
  BufferAttribute,
  Color,
  ConeGeometry,
  CylinderGeometry,
  InstancedMesh,
  MeshStandardMaterial,
  Object3D,
  SphereGeometry,
  TorusGeometry,
  type BufferGeometry,
} from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { nonIndexed } from './geom'
import type { ThreeEvent } from '@react-three/fiber'
import { GADGET_SHAPES, type GadgetItem, type GadgetShape } from '../protocol'
import { useStore } from '../state/store'
import { attachInstanceAttributes, makeInstancedMaterial } from './instancedMaterial'
import { heightAt, terrainState } from './terrain'
import { useTerrainRev } from './useTerrainRev'

const COLOR_RE = /^#[0-9a-f]{6}$/i
const FALLBACK_COLOR = '#8b93a7'
const PEDESTAL_H = 0.22
const dummy = new Object3D()
const color = new Color()

export function safeGadgetColor(c: string): string {
  return COLOR_RE.test(c) ? c : FALLBACK_COLOR
}

function isShape(s: unknown): s is GadgetShape {
  return typeof s === 'string' && (GADGET_SHAPES as readonly string[]).includes(s)
}

function geometryFor(shape: GadgetShape): BufferGeometry {
  switch (shape) {
    case 'cube': {
      const g = new BoxGeometry(1, 1, 1)
      g.translate(0, 0.5, 0)
      return g
    }
    case 'sphere': {
      const g = new SphereGeometry(0.5, 18, 12)
      g.translate(0, 0.5, 0)
      return g
    }
    case 'pyramid': {
      const g = new ConeGeometry(0.7, 1, 4)
      g.translate(0, 0.5, 0)
      return g
    }
    case 'cylinder': {
      const g = new CylinderGeometry(0.45, 0.45, 1, 18)
      g.translate(0, 0.5, 0)
      return g
    }
    case 'torus': {
      const g = new TorusGeometry(0.45, 0.16, 10, 26)
      g.rotateX(Math.PI / 2)
      g.translate(0, 0.25, 0)
      return g
    }
  }
}

function paint(g: BufferGeometry, hex: string): BufferGeometry {
  const n = g.getAttribute('position').count
  const c = new Color(hex)
  const arr = new Float32Array(n * 3)
  for (let i = 0; i < n; i++) {
    arr[i * 3] = c.r
    arr[i * 3 + 1] = c.g
    arr[i * 3 + 2] = c.b
  }
  g.setAttribute('color', new BufferAttribute(arr, 3))
  return g
}

/** Dark stone disc with a bright rim: the rim glows in the gadget's colour through the instance tint. */
function pedestalGeometry(): BufferGeometry {
  const base = paint(new CylinderGeometry(0.85, 0.98, PEDESTAL_H, 22), '#2a2f3a')
  base.translate(0, PEDESTAL_H / 2, 0)
  const rim = paint(new TorusGeometry(0.84, 0.045, 8, 36), '#ffffff')
  rim.rotateX(Math.PI / 2)
  rim.translate(0, PEDESTAL_H + 0.01, 0)
  const merged = mergeGeometries([nonIndexed(base), nonIndexed(rim)], false)!
  merged.computeVertexNormals()
  merged.computeBoundingSphere()
  return merged
}

const geometries = new Map<GadgetShape, BufferGeometry>()
for (const s of GADGET_SHAPES) geometries.set(s, geometryFor(s))
const shapeMaterial = new MeshStandardMaterial({ roughness: 0.35, metalness: 0.25 })
const pedestalGeom = pedestalGeometry()
const pedestalMat = (() => {
  const m = makeInstancedMaterial({ roughness: 0.8, metalness: 0.05, emissiveScale: 1.4 })
  m.vertexColors = true
  return m
})()

interface ShapeGroupProps {
  shape: GadgetShape
  items: GadgetItem[]
}

const ShapeGroup = memo(function ShapeGroup({ shape, items }: ShapeGroupProps) {
  const rev = useTerrainRev()
  const mesh = useMemo(() => {
    void rev
    const m = new InstancedMesh(geometries.get(shape)!, shapeMaterial, items.length)
    m.frustumCulled = false
    for (let i = 0; i < items.length; i++) {
      const it = items[i]!
      const r = it.render
      const sc = Math.min(3, Math.max(0.3, Number.isFinite(r.scale) ? r.scale : 1))
      const lift = Number.isFinite(r.height_offset ?? 0) ? Math.max(0, r.height_offset ?? 0) : 0
      dummy.position.set(it.x, heightAt(it.x, it.y) + PEDESTAL_H + lift, it.y)
      dummy.rotation.set(0, ((r.rotation_deg ?? 0) * Math.PI) / 180, 0)
      dummy.scale.setScalar(sc)
      dummy.updateMatrix()
      m.setMatrixAt(i, dummy.matrix)
      m.setColorAt(i, color.set(safeGadgetColor(r.color)))
    }
    m.instanceMatrix.needsUpdate = true
    if (m.instanceColor) m.instanceColor.needsUpdate = true
    return m
  }, [shape, items, rev])
  useEffect(() => () => mesh.dispose(), [mesh])

  const onMove = (e: ThreeEvent<PointerEvent>) => {
    if (e.instanceId === undefined) return
    e.stopPropagation()
    useStore.getState().hoverGadget(items[e.instanceId]?.id ?? null)
  }
  const onOut = () => useStore.getState().hoverGadget(null)
  return <primitive object={mesh} onPointerMove={onMove} onPointerOut={onOut} />
})

const Pedestals = memo(function Pedestals({ items }: { items: GadgetItem[] }) {
  const rev = useTerrainRev()
  const mesh = useMemo(() => {
    void rev
    const n = Math.max(1, items.length)
    const geom = pedestalGeom.clone()
    const attrs = attachInstanceAttributes(geom, n)
    const m = new InstancedMesh(geom, pedestalMat, n)
    m.frustumCulled = false
    const pulse = attrs.pulse.array as Float32Array
    for (let i = 0; i < items.length; i++) {
      const it = items[i]!
      const sc = Math.min(3, Math.max(0.3, Number.isFinite(it.render.scale) ? it.render.scale : 1))
      dummy.position.set(it.x, heightAt(it.x, it.y) - 0.02, it.y)
      dummy.rotation.set(0, 0, 0)
      dummy.scale.set(0.7 + 0.35 * sc, 1, 0.7 + 0.35 * sc)
      dummy.updateMatrix()
      m.setMatrixAt(i, dummy.matrix)
      m.setColorAt(i, color.set(safeGadgetColor(it.render.color)))
      pulse[i] = 0.55
    }
    m.count = items.length
    m.instanceMatrix.needsUpdate = true
    if (m.instanceColor) m.instanceColor.needsUpdate = true
    attrs.pulse.needsUpdate = true
    return m
  }, [items, rev])
  useEffect(
    () => () => {
      mesh.geometry.dispose()
      mesh.dispose()
    },
    [mesh],
  )
  if (items.length === 0) return null
  return <primitive object={mesh} />
})

/** One instanced mesh per shape in the closed enum plus one for the pedestals, rebuilt on gadgets_rev. */
export const Gadgets = memo(function Gadgets() {
  const gadgets = useStore((s) => s.gadgets)
  const groups = useMemo(() => {
    const by = new Map<GadgetShape, GadgetItem[]>()
    for (const it of gadgets?.items ?? []) {
      const shape = it.render?.shape
      if (!isShape(shape)) continue
      let list = by.get(shape)
      if (!list) {
        list = []
        by.set(shape, list)
      }
      list.push(it)
    }
    return Array.from(by.entries())
  }, [gadgets])
  const valid = useMemo(() => (gadgets?.items ?? []).filter((it) => isShape(it.render?.shape)), [gadgets])
  void terrainState
  return (
    <group>
      {groups.map(([shape, items]) => (
        <ShapeGroup key={shape + ':' + (gadgets?.rev ?? 0)} shape={shape} items={items} />
      ))}
      <Pedestals items={valid} />
    </group>
  )
})
