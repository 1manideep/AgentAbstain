/**
 * Optional GLB prop pack. When `web/public/models/manifest.json` exists, the
 * instanced procedural props are swapped for the pack's meshes (same instance
 * transforms, same draw-call budget). Without the file this is a no-op.
 *
 * manifest.json:
 *   { "props": { "tree_pine": "tree_pine.glb", "tree_round": "...", "tree_bush": "...", "rock": "...", "grass": "..." },
 *     "rig": "rig.glb" }
 * Each GLB's first Mesh is used; its geometry should be authored with the
 * base on y = 0 and roughly 1 unit tall so the procedural scales still apply.
 */
import { useEffect, useState } from 'react'
import { type BufferGeometry, type Material, Mesh } from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

export interface PackManifest {
  props?: Record<string, string>
  rig?: string
}

export interface PackEntry {
  geometry: BufferGeometry
  material: Material
}

export type PropPack = Record<string, PackEntry>

let manifestPromise: Promise<PackManifest | null> | null = null

export function loadManifest(): Promise<PackManifest | null> {
  if (!manifestPromise) {
    manifestPromise = fetch('/models/manifest.json', { credentials: 'same-origin' })
      .then(async (r) => {
        if (!r.ok) return null
        const ct = r.headers.get('content-type') ?? ''
        if (!ct.includes('json')) return null
        const j = (await r.json()) as PackManifest
        return j && typeof j === 'object' ? j : null
      })
      .catch(() => null)
  }
  return manifestPromise
}

async function loadEntry(loader: GLTFLoader, file: string): Promise<PackEntry | null> {
  try {
    const gltf = await loader.loadAsync('/models/' + file)
    let found: Mesh | null = null
    gltf.scene.traverse((o) => {
      if (!found && (o as Mesh).isMesh) found = o as Mesh
    })
    if (!found) return null
    const m = found as Mesh
    const material = Array.isArray(m.material) ? m.material[0]! : m.material
    return { geometry: m.geometry, material }
  } catch {
    return null
  }
}

/** Resolves to the loaded pack (keys from the manifest) or null when there is no pack. */
export function useGLTFPack(): PropPack | null {
  const [pack, setPack] = useState<PropPack | null>(null)
  useEffect(() => {
    let cancelled = false
    void loadManifest().then(async (m) => {
      if (!m || !m.props || cancelled) return
      const loader = new GLTFLoader()
      const out: PropPack = {}
      await Promise.all(
        Object.entries(m.props).map(async ([key, file]) => {
          const e = await loadEntry(loader, file)
          if (e) out[key] = e
        }),
      )
      if (!cancelled && Object.keys(out).length) setPack(out)
    })
    return () => {
      cancelled = true
    }
  }, [])
  return pack
}
