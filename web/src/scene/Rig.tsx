/**
 * Optional character rig (ReadyPlayerMe / Mixamo style GLB). `useRig(url)`
 * loads `web/public/models/rig.glb` when it exists (or the manifest's `rig`),
 * otherwise it is a no-op and the instanced capsules stay. With a rig, up to
 * `rigState.maxRigged` visible agents get a SkeletonUtils clone with an
 * AnimationMixer; the procedural weight vector {idle, walk, sleep, degenerate,
 * dead} drives the matching clips' weights. Beyond the cap, agents remain
 * instanced capsules. Clip names are matched by regex (see CLIP_PATTERNS).
 */
import { useEffect, useMemo, useState } from 'react'
import { AnimationClip, AnimationMixer, Group, Object3D, type AnimationAction } from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { clone as skeletonClone } from 'three/examples/jsm/utils/SkeletonUtils.js'
import { loadManifest } from './gltfPack'
import { rigState } from './rigState'
import { perSlot, registerSystem, slotColor, SYS_RIG, type FrameCtx } from './sceneState'

export interface LoadedRig {
  scene: Group
  clips: AnimationClip[]
}

const CLIP_PATTERNS: Array<[RegExp, number]> = [
  [/idle|breath|stand/i, 0],
  [/walk|run|jog/i, 1],
  [/sleep|lie|rest|lay/i, 2],
  [/degenerat|twitch|dance|shake|crazy|glitch/i, 3],
  [/dead|death|die|fall/i, 4],
]

export function useRig(url = '/models/rig.glb'): LoadedRig | null {
  const [rig, setRig] = useState<LoadedRig | null>(null)
  useEffect(() => {
    let cancelled = false
    void (async () => {
      const manifest = await loadManifest()
      const file = manifest?.rig ? '/models/' + manifest.rig : url
      try {
        const head = await fetch(file, { method: 'HEAD', credentials: 'same-origin' })
        const ct = head.headers.get('content-type') ?? ''
        if (!head.ok || ct.includes('text/html')) return
        const gltf = await new GLTFLoader().loadAsync(file)
        if (cancelled) return
        setRig({ scene: gltf.scene, clips: gltf.animations })
      } catch {
        /* no rig: instanced capsules remain */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [url])
  return rig
}

interface Puppet {
  root: Object3D
  mixer: AnimationMixer
  actions: (AnimationAction | null)[]
  slot: number
}

function RiggedAgents({ rig }: { rig: LoadedRig }) {
  const group = useMemo(() => new Group(), [])
  const pool = useMemo(() => {
    const puppets: Puppet[] = []
    for (let i = 0; i < rigState.maxRigged; i++) {
      const root = skeletonClone(rig.scene)
      root.visible = false
      const mixer = new AnimationMixer(root)
      const actions: (AnimationAction | null)[] = [null, null, null, null, null]
      for (const [re, idx] of CLIP_PATTERNS) {
        if (actions[idx]) continue
        const clip = rig.clips.find((c) => re.test(c.name))
        if (clip) {
          const a = mixer.clipAction(clip)
          a.play()
          a.setEffectiveWeight(0)
          actions[idx] = a
        }
      }
      group.add(root)
      puppets.push({ root, mixer, actions, slot: -1 })
    }
    return puppets
  }, [rig, group])

  useEffect(() => {
    rigState.glbActive = true // the GLB wins over the procedural characters
    const assigned = new Map<number, Puppet>()
    const system = (ctx: FrameCtx) => {
      const { out, dt, speed } = ctx
      // release puppets whose agent is gone; assign free puppets to visible slots in order
      for (const [slot, pup] of assigned) {
        if (slot >= out.count || !out.present[slot]) {
          assigned.delete(slot)
          pup.slot = -1
          pup.root.visible = false
        }
      }
      rigState.riggedSlots.clear()
      let free = pool.filter((p) => p.slot < 0)
      for (let s = 0; s < out.count && (assigned.has(s) || free.length); s++) {
        if (!out.present[s]) continue
        let pup = assigned.get(s)
        if (!pup) {
          pup = free.shift()!
          pup.slot = s
          assigned.set(s, pup)
          pup.root.visible = true
        }
        rigState.riggedSlots.add(s)
        const x = out.x[s]!
        const z = out.y[s]!
        pup.root.position.set(x, ctx.heightAt(x, z), z)
        pup.root.rotation.set(0, -out.heading[s]! + Math.PI / 2, 0)
        const sc = out.scale[s]!
        pup.root.scale.setScalar(sc)
        const w = perSlot.weights
        const b = s * 5
        for (let k = 0; k < 5; k++) pup.actions[k]?.setEffectiveWeight(w[b + k]!)
        // tint any material with the tier colour so tiers stay legible on shared rigs
        const c = slotColor(s, out.ids[s]!)
        pup.root.traverse((o) => {
          const m = (o as { material?: { color?: { copy: (c: unknown) => void } } }).material
          if (m && m.color) m.color.copy(c)
        })
        pup.mixer.update(dt * Math.max(0.05, Math.min(2, speed)))
      }
      free = []
    }
    const unregister = registerSystem(SYS_RIG, system)
    return () => {
      unregister()
      rigState.glbActive = false
    }
  }, [pool])

  useEffect(() => () => rigState.riggedSlots.clear(), [])
  return <primitive object={group} />
}

/** Mounts the rig layer when a rig file exists; otherwise renders nothing. */
export function Rig() {
  const rig = useRig()
  if (!rig) return null
  return <RiggedAgents rig={rig} />
}
