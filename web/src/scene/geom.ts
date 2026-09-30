import type { BufferGeometry } from 'three'

/** `toNonIndexed()` warns on geometry that is already non-indexed (polyhedra); merge inputs must all match. */
export function nonIndexed(g: BufferGeometry): BufferGeometry {
  return g.index ? g.toNonIndexed() : g
}
