/**
 * Effects layer: speech bubbles and the hover tag (drei <Text> billboards),
 * selection ring, transfer arcs (one LineSegments), particle bursts (one
 * Points) and per-slot flicker/flash timers. Effects are fired from the
 * pendingFx heap when the render clock reaches their tick, never on arrival.
 */
import { Billboard, Text } from '@react-three/drei'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  Color,
  DynamicDrawUsage,
  Group,
  LineBasicMaterial,
  LineSegments,
  Mesh,
  MeshBasicMaterial,
  Points,
  RingGeometry,
  ShaderMaterial,
} from 'three'
import type { EventMsg } from '../protocol'
import { isEventKind } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { fxQueue } from './fx'
import { nodePulse } from './Nodes'
import { mirror, perSlot, registerSystem, SYS_EFFECTS, type FrameCtx } from './sceneState'

export const FONT_URL = '/fonts/InstrumentSans-Regular.ttf'
const BUBBLE_MS = 4000
const FADE_MS = 300
const MAX_BUBBLES = 8
const MAX_PER_AGENT = 2
const MAX_ARCS = 16
const ARC_SEGS = 20
const ARC_MS = 1300
const MAX_BURSTS = 20
const PARTICLES = 14

interface Bubble {
  key: number
  agentId: string
  text: string
  born: number
  lane: number
  group: Group | null
  troika: { fillOpacity: number; outlineOpacity: number } | null
}

interface Arc {
  from: string
  to: string
  x0: number
  z0: number
  x1: number
  z1: number
  t0: number
}

interface Burst {
  x: number
  y: number
  z: number
  t0: number
  dur: number
  r: number
  g: number
  b: number
  spread: number
  lift: number
  gravity: number
}

function hash(n: number): number {
  const x = Math.sin(n * 12.9898 + 78.233) * 43758.5453
  return x - Math.floor(x)
}

function agentPos(id: string, out: FrameCtx['out']): { x: number; z: number; ok: boolean } {
  const s = history.agentIndex.get(id)
  if (s === undefined || s >= out.count || !out.present[s]) return { x: 0, z: 0, ok: false }
  return { x: out.x[s]!, z: out.y[s]!, ok: true }
}

const bubbleState = { list: [] as Bubble[], nextKey: 1 }
const arcs: Arc[] = []
const bursts: Burst[] = []

const BubbleView = memo(function BubbleView({ bubble }: { bubble: Bubble }) {
  const setGroup = useCallback(
    (g: Group | null) => {
      bubble.group = g
    },
    [bubble],
  )
  const onSync = useCallback(
    (t: { fillOpacity: number; outlineOpacity: number }) => {
      bubble.troika = t
    },
    [bubble],
  )
  return (
    <group ref={setGroup} visible={false}>
      <Billboard follow>
        <Text
          font={FONT_URL}
          fontSize={0.62}
          maxWidth={10}
          lineHeight={1.15}
          textAlign="center"
          anchorX="center"
          anchorY="bottom"
          color="#eef2ff"
          outlineWidth={0.05}
          outlineColor="#060911"
          outlineOpacity={0.95}
          fillOpacity={0}
          onSync={onSync}
        >
          {bubble.text}
        </Text>
      </Billboard>
    </group>
  )
})

const HoverTag = memo(function HoverTag() {
  const hoveredId = useStore((s) => s.hoveredAgentId)
  const hoveredGadget = useStore((s) => s.hoveredGadgetId)
  const rosterById = useStore((s) => s.rosterById)
  const gadgets = useStore((s) => s.gadgets)
  const ref = useRef<Group>(null)
  const label = useMemo(() => {
    if (hoveredId) {
      const a = rosterById.get(hoveredId)
      return a ? `${a.name} · ${a.tier} · g${a.generation}` : hoveredId
    }
    if (hoveredGadget) {
      const g = gadgets?.items.find((i) => i.id === hoveredGadget)
      return g ? `${g.render.label || g.name} · ${g.uses} uses` : ''
    }
    return ''
  }, [hoveredId, hoveredGadget, rosterById, gadgets])
  const gadgetPos = useMemo(() => {
    const g = hoveredGadget ? gadgets?.items.find((i) => i.id === hoveredGadget) : undefined
    return g ? { x: g.x, z: g.y, h: (g.render.height_offset ?? 0) + Math.min(3, Math.max(0.3, g.render.scale)) } : null
  }, [hoveredGadget, gadgets])
  useEffect(() => {
    const system = (ctx: FrameCtx) => {
      const g = ref.current
      if (!g) return
      if (hoveredId) {
        const p = agentPos(hoveredId, ctx.out)
        g.visible = p.ok && label !== ''
        if (p.ok) g.position.set(p.x, 2.15, p.z)
      } else if (gadgetPos) {
        g.visible = label !== ''
        g.position.set(gadgetPos.x, gadgetPos.h + 0.4, gadgetPos.z)
      } else g.visible = false
    }
    return registerSystem(SYS_EFFECTS + 1, system)
  }, [hoveredId, gadgetPos, label])
  if (!label) return null
  return (
    <group ref={ref} visible={false}>
      <Billboard follow>
        <Text
          font={FONT_URL}
          fontSize={0.55}
          anchorX="center"
          anchorY="bottom"
          color="#ffffff"
          outlineWidth={0.045}
          outlineColor="#060911"
        >
          {label}
        </Text>
      </Billboard>
    </group>
  )
})

export function Effects() {
  const [bubbles, setBubbles] = useState<Bubble[]>([])
  const ringRef = useRef<Mesh>(null)

  const ring = useMemo(() => {
    const g = new RingGeometry(0.9, 1.1, 40)
    g.rotateX(-Math.PI / 2)
    const m = new MeshBasicMaterial({ color: '#dfe7ff', transparent: true, opacity: 0.85, depthWrite: false })
    return { g, m }
  }, [])

  const arcObj = useMemo(() => {
    const geom = new BufferGeometry()
    const pos = new BufferAttribute(new Float32Array(MAX_ARCS * ARC_SEGS * 2 * 3), 3)
    pos.setUsage(DynamicDrawUsage)
    geom.setAttribute('position', pos)
    geom.setDrawRange(0, 0)
    const mat = new LineBasicMaterial({ color: '#ffd166', transparent: true, opacity: 0.95, depthWrite: false })
    const lines = new LineSegments(geom, mat)
    lines.frustumCulled = false
    return { geom, pos, lines }
  }, [])

  const pointsObj = useMemo(() => {
    const n = MAX_BURSTS * PARTICLES
    const geom = new BufferGeometry()
    const pos = new BufferAttribute(new Float32Array(n * 3), 3)
    pos.setUsage(DynamicDrawUsage)
    const col = new BufferAttribute(new Float32Array(n * 4), 4)
    col.setUsage(DynamicDrawUsage)
    const size = new BufferAttribute(new Float32Array(n), 1)
    size.setUsage(DynamicDrawUsage)
    geom.setAttribute('position', pos)
    geom.setAttribute('aColor', col)
    geom.setAttribute('aSize', size)
    geom.setDrawRange(0, 0)
    const mat = new ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
      vertexShader: /* glsl */ `
        attribute vec4 aColor; attribute float aSize; varying vec4 vColor;
        void main() {
          vColor = aColor;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = aSize * (180.0 / -mv.z);
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: /* glsl */ `
        varying vec4 vColor;
        void main() {
          vec2 c = gl_PointCoord - 0.5;
          float d = length(c);
          float a = smoothstep(0.5, 0.15, d) * vColor.a;
          gl_FragColor = vec4(vColor.rgb, a);
        }`,
    })
    const points = new Points(geom, mat)
    points.frustumCulled = false
    return { geom, pos, col, size, points }
  }, [])

  useEffect(() => () => {
    ring.g.dispose()
    ring.m.dispose()
    arcObj.geom.dispose()
    pointsObj.geom.dispose()
  }, [ring, arcObj, pointsObj])

  useEffect(() => {
    let dirtyBubbles = false
    const c = new Color()

    const addBurst = (x: number, z: number, y: number, hex: string, spread: number, lift: number, gravity: number, dur: number, now: number) => {
      c.set(hex)
      if (bursts.length >= MAX_BURSTS) bursts.shift()
      bursts.push({ x, y, z, t0: now, dur, r: c.r, g: c.g, b: c.b, spread, lift, gravity })
    }

    const fire = (e: EventMsg, ctx: FrameCtx) => {
      const { out, now } = ctx
      if (isEventKind(e, 'talk')) {
        const p = agentPos(e.payload.speaker_id, out)
        if (!p.ok) return
        const text = String(e.payload.text ?? '').slice(0, 280)
        const mine = bubbleState.list.filter((b) => b.agentId === e.payload.speaker_id)
        // The same line repeated (scripted brains do this): refresh the bubble instead of stacking a twin.
        const twin = mine.find((b) => b.text === text)
        if (twin) {
          twin.born = now
          return
        }
        // At most two bubbles per speaker; the oldest makes room.
        while (mine.length >= MAX_PER_AGENT) {
          const oldest = mine.shift()!
          const i = bubbleState.list.indexOf(oldest)
          if (i >= 0) bubbleState.list.splice(i, 1)
        }
        for (let i = 0; i < mine.length; i++) mine[i]!.lane = i + 1
        bubbleState.list.push({ key: bubbleState.nextKey++, agentId: e.payload.speaker_id, text, born: now, lane: 0, group: null, troika: null })
        while (bubbleState.list.length > MAX_BUBBLES) bubbleState.list.shift()
        dirtyBubbles = true
        return
      }
      if (isEventKind(e, 'transfer')) {
        const a = agentPos(e.payload.from, out)
        const b = agentPos(e.payload.to, out)
        if (!a.ok || !b.ok) return
        if (arcs.length >= MAX_ARCS) arcs.shift()
        arcs.push({ from: e.payload.from, to: e.payload.to, x0: a.x, z0: a.z, x1: b.x, z1: b.z, t0: now })
        const s = history.agentIndex.get(e.payload.to)
        if (s !== undefined) perSlot.flashUntil[s] = now + ARC_MS / 1000 + 0.4
        return
      }
      if (isEventKind(e, 'windfall')) {
        const p = agentPos(e.payload.agent_id, out)
        if (!p.ok) return
        addBurst(p.x, p.z, 0.9, '#ffd166', 1.1, 2.6, 3.2, 1.6, now)
        const s = history.agentIndex.get(e.payload.agent_id)
        if (s !== undefined) perSlot.flashUntil[s] = now + 1.2
        return
      }
      if (isEventKind(e, 'birth')) {
        const p = agentPos(e.payload.agent_id, out)
        const s = history.agentIndex.get(e.payload.agent_id)
        const tier = mirror.rosterById.get(e.payload.agent_id)?.tier
        const hex = (tier && mirror.tierColor.get(tier)?.getHexString()) || '4fd1c5'
        if (p.ok) addBurst(p.x, p.z, 0.4, '#' + hex, 1.6, 1.4, 1.5, 1.4, now)
        if (s !== undefined) perSlot.flashUntil[s] = now + 1.0
        return
      }
      if (isEventKind(e, 'death')) {
        const p = agentPos(e.payload.agent_id, out)
        if (p.ok) addBurst(p.x, p.z, 0.8, '#6b7280', 0.7, -0.6, -0.8, 1.2, now)
        return
      }
      if (isEventKind(e, 'degeneration')) {
        const s = history.agentIndex.get(e.agent_id ?? '')
        if (s !== undefined) perSlot.degenUntil[s] = now + 1.8
        return
      }
      if (isEventKind(e, 'forage')) {
        const ns = history.nodeIndex.get(e.payload.node_id)
        if (ns !== undefined && ns < nodePulse.until.length) nodePulse.until[ns] = Math.max(nodePulse.until[ns]!, now + 0.7)
        const p = agentPos(e.payload.agent_id, out)
        if (p.ok) addBurst(p.x, p.z, 0.5, '#2fbf8f', 0.35, 1.2, 1.8, 0.8, now)
        return
      }
      if (isEventKind(e, 'gadget_verified')) {
        const g = useStore.getState().gadgets?.items.find((i) => i.id === e.payload.gadget_id)
        if (g) addBurst(g.x, g.y, 0.8, '#8ce99a', 1.3, 1.6, 1.2, 1.5, now)
      }
    }

    const system = (ctx: FrameCtx) => {
      const { out, now } = ctx
      fxQueue.drain(out.tick, (e) => fire(e, ctx))

      // bubbles: position over the speaker, stacked, fade in/out
      const list = bubbleState.list
      for (let i = list.length - 1; i >= 0; i--) {
        const b = list[i]!
        const age = (now - b.born) * 1000
        if (age > BUBBLE_MS + FADE_MS) {
          list.splice(i, 1)
          dirtyBubbles = true
          continue
        }
        const p = agentPos(b.agentId, out)
        if (b.group) {
          b.group.visible = p.ok
          if (p.ok) b.group.position.set(p.x, 2.35 + b.lane * 2.1 + Math.min(1, age / 600) * 0.2, p.z)
        }
        if (b.troika) {
          const fin = Math.min(1, age / FADE_MS)
          const fout = age > BUBBLE_MS ? 1 - (age - BUBBLE_MS) / FADE_MS : 1
          const o = Math.max(0, Math.min(fin, fout))
          b.troika.fillOpacity = o
          b.troika.outlineOpacity = o * 0.95
        }
      }
      if (dirtyBubbles) {
        dirtyBubbles = false
        setBubbles(list.slice())
      }

      // selection ring
      const ringMesh = ringRef.current
      if (ringMesh) {
        const sel = mirror.selectedId
        const p = sel ? agentPos(sel, out) : null
        ringMesh.visible = !!(p && p.ok)
        if (p && p.ok) {
          ringMesh.position.set(p.x, 0.03, p.z)
          const k = 1 + 0.06 * Math.sin(now * 3)
          ringMesh.scale.set(k, 1, k)
        }
      }

      // transfer arcs
      const ap = arcObj.pos.array as Float32Array
      let v = 0
      for (let i = arcs.length - 1; i >= 0; i--) {
        const a = arcs[i]!
        const u = ((now - a.t0) * 1000) / ARC_MS
        if (u > 1.2) {
          arcs.splice(i, 1)
          continue
        }
        const pa = agentPos(a.from, out)
        const pb = agentPos(a.to, out)
        const x0 = pa.ok ? pa.x : a.x0
        const z0 = pa.ok ? pa.z : a.z0
        const x1 = pb.ok ? pb.x : a.x1
        const z1 = pb.ok ? pb.z : a.z1
        const head = Math.min(1, u)
        const tail = Math.max(0, u - 0.3)
        const h = 1.2 + Math.hypot(x1 - x0, z1 - z0) * 0.25
        for (let s = 0; s < ARC_SEGS; s++) {
          const t0 = tail + ((head - tail) * s) / ARC_SEGS
          const t1 = tail + ((head - tail) * (s + 1)) / ARC_SEGS
          ap[v++] = x0 + (x1 - x0) * t0
          ap[v++] = 0.9 + Math.sin(t0 * Math.PI) * h
          ap[v++] = z0 + (z1 - z0) * t0
          ap[v++] = x0 + (x1 - x0) * t1
          ap[v++] = 0.9 + Math.sin(t1 * Math.PI) * h
          ap[v++] = z0 + (z1 - z0) * t1
        }
      }
      arcObj.geom.setDrawRange(0, v / 3)
      arcObj.lines.visible = v > 0
      if (v > 0) arcObj.pos.needsUpdate = true

      // particle bursts
      const pp = pointsObj.pos.array as Float32Array
      const pc = pointsObj.col.array as Float32Array
      const ps = pointsObj.size.array as Float32Array
      let n = 0
      for (let i = bursts.length - 1; i >= 0; i--) {
        const b = bursts[i]!
        const t = now - b.t0
        if (t > b.dur) {
          bursts.splice(i, 1)
          continue
        }
        const k = t / b.dur
        for (let j = 0; j < PARTICLES; j++) {
          const seed = i * 131 + j * 7 + b.t0 * 13
          const ang = hash(seed) * Math.PI * 2
          const sp = (0.4 + hash(seed + 1) * 0.6) * b.spread
          const up = (0.5 + hash(seed + 2)) * b.lift
          pp[n * 3] = b.x + Math.cos(ang) * sp * t
          pp[n * 3 + 1] = b.y + up * t - 0.5 * b.gravity * t * t
          pp[n * 3 + 2] = b.z + Math.sin(ang) * sp * t
          const a = (1 - k) * (1 - k)
          pc[n * 4] = b.r
          pc[n * 4 + 1] = b.g
          pc[n * 4 + 2] = b.b
          pc[n * 4 + 3] = a
          ps[n] = 0.18 + 0.12 * hash(seed + 3)
          n++
        }
      }
      pointsObj.geom.setDrawRange(0, n)
      pointsObj.points.visible = n > 0
      if (n > 0) {
        pointsObj.pos.needsUpdate = true
        pointsObj.col.needsUpdate = true
        pointsObj.size.needsUpdate = true
      }
    }
    return registerSystem(SYS_EFFECTS, system)
  }, [arcObj, pointsObj])

  return (
    <group>
      {bubbles.map((b) => (
        <BubbleView key={b.key} bubble={b} />
      ))}
      <HoverTag />
      <mesh ref={ringRef} geometry={ring.g} material={ring.m} visible={false} />
      <primitive object={arcObj.lines} />
      <primitive object={pointsObj.points} />
    </group>
  )
}
