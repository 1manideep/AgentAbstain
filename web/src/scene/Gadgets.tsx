import { memo, useEffect, useMemo } from 'react'
import {
  BoxGeometry,
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
import type { ThreeEvent } from '@react-three/fiber'
import { GADGET_SHAPES, type GadgetItem, type GadgetShape } from '../protocol'
import { useStore } from '../state/store'

const COLOR_RE = /^#[0-9a-f]{6}$/i
const FALLBACK_COLOR = '#8b93a7'
const dummy = new Object3D()
const color = new Color()

export function safeGadgetColor(c: string): string {
  return COLOR_RE.test(c) ? c : FALLBACK_COLOR
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

const geometries = new Map<GadgetShape, BufferGeometry>()
for (const s of GADGET_SHAPES) geometries.set(s, geometryFor(s))
const material = new MeshStandardMaterial({ roughness: 0.35, metalness: 0.25 })

interface ShapeGroupProps {
  shape: GadgetShape
  items: GadgetItem[]
}

const ShapeGroup = memo(function ShapeGroup({ shape, items }: ShapeGroupProps) {
  const mesh = useMemo(() => {
    const m = new InstancedMesh(geometries.get(shape)!, material, items.length)
    m.frustumCulled = false
    for (let i = 0; i < items.length; i++) {
      const it = items[i]!
      const r = it.render
      const sc = Math.min(3, Math.max(0.3, Number.isFinite(r.scale) ? r.scale : 1))
      dummy.position.set(it.x, Number.isFinite(r.height_offset ?? 0) ? Math.max(0, r.height_offset ?? 0) : 0, it.y)
      dummy.rotation.set(0, ((r.rotation_deg ?? 0) * Math.PI) / 180, 0)
      dummy.scale.setScalar(sc)
      dummy.updateMatrix()
      m.setMatrixAt(i, dummy.matrix)
      m.setColorAt(i, color.set(safeGadgetColor(r.color)))
    }
    m.instanceMatrix.needsUpdate = true
    if (m.instanceColor) m.instanceColor.needsUpdate = true
    return m
  }, [shape, items])
  useEffect(() => () => mesh.dispose(), [mesh])

  const onMove = (e: ThreeEvent<PointerEvent>) => {
    if (e.instanceId === undefined) return
    e.stopPropagation()
    useStore.getState().hoverGadget(items[e.instanceId]?.id ?? null)
  }
  const onOut = () => useStore.getState().hoverGadget(null)
  return <primitive object={mesh} onPointerMove={onMove} onPointerOut={onOut} />
})

/** One instanced mesh per shape in the closed enum, rebuilt on gadgets_rev. */
export const Gadgets = memo(function Gadgets() {
  const gadgets = useStore((s) => s.gadgets)
  const groups = useMemo(() => {
    const by = new Map<GadgetShape, GadgetItem[]>()
    for (const it of gadgets?.items ?? []) {
      const shape = it.render?.shape
      if (!GADGET_SHAPES.includes(shape)) continue
      let list = by.get(shape)
      if (!list) {
        list = []
        by.set(shape, list)
      }
      list.push(it)
    }
    return Array.from(by.entries())
  }, [gadgets])
  return (
    <group>
      {groups.map(([shape, items]) => (
        <ShapeGroup key={shape + ':' + (gadgets?.rev ?? 0)} shape={shape} items={items} />
      ))}
    </group>
  )
})
