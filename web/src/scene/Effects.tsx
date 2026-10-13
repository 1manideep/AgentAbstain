/**
 * Effects layer: speech bubbles (compact drei <Text> + one instanced rounded
 * backdrop), the hover tag, selection ring, transfer arcs (one LineSegments),
 * particle bursts (one Points) and per-slot flicker/flash timers. Effects are
 * fired from the pendingFx heap when the render clock reaches their tick,
 * never on arrival. No per-frame allocation: bubbles reuse their objects, the
 * arc/particle buffers are fixed, and visibility selection is done in place.
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
  InstancedBufferAttribute,
  InstancedMesh,
  LineBasicMaterial,
  LineSegments,
  Mesh,
  MeshBasicMaterial,
  Object3D,
  Points,
  RingGeometry,
  ShaderMaterial,
  Shape,
  ShapeGeometry,
  Vector3,
} from 'three'
import type { EventMsg } from '../protocol'
import { isEventKind } from '../protocol'
import { history } from '../state/history'
import { useStore } from '../state/store'
import { BUBBLE_LINE_HEIGHT, clampBubbleText, clampNoteText } from './bubbleText'
import { BUBBLE_MS, bubbleStyle, MARK_MS, NOTE_MS, type BubbleKind } from './bubbleStyle'
import { fxQueue } from './fx'
import { nodePulse } from './Nodes'
import { mirror, perSlot, registerSystem, SYS_EFFECTS, type FrameCtx } from './sceneState'

export const FONT_URL = '/fonts/InstrumentSans-Regular.ttf'
const FADE_MS = 300
const MAX_VISIBLE_BUBBLES = 6
const MAINTENANCE_MS = 3000
/** A mark (✗) sits beside the head, offset along the camera's right vector, so it never covers a speech bubble. */
const MARK_SIDE = 0.75
const BUBBLE_MAX_DIST = 35
const SPEAK_COOLDOWN_MS = 1500
const BUBBLE_PAD = 0.14
const MAX_ARCS = 16
const ARC_SEGS = 20
const ARC_MS = 1300
const MAX_BURSTS = 20
const PARTICLES = 14

interface TroikaText {
  fillOpacity: number
  outlineOpacity: number
  textRenderInfo?: { blockBounds?: [number, number, number, number] }
}

interface Bubble {
  agentId: string
  kind: BubbleKind
  /** The kernel recorded at least one false claim in this speech. */
  lied: boolean
  ttlMs: number
  text: string
  born: number
  /** bumped when the text changes so the memoised view re-renders */
  rev: number
  group: Group | null
  troika: TroikaText | null
  /** measured text block size (world units) after troika sync */
  w: number
  h: number
  /** scratch for visibility selection */
  dist: number
  x: number
  y: number
  z: number
  ok: boolean
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

const posScratch = { x: 0, y: 0, z: 0, ok: false }

/** Agent position at render time (world x, terrain height y, world z). Reuses one scratch object. */
function agentPos(id: string, ctx: FrameCtx): { x: number; y: number; z: number; ok: boolean } {
  const out = ctx.out
  const s = history.agentIndex.get(id)
  if (s === undefined || s >= out.count || !out.present[s]) {
    posScratch.ok = false
    return posScratch
  }
  posScratch.x = out.x[s]!
  posScratch.z = out.y[s]!
  posScratch.y = ctx.heightAt(posScratch.x, posScratch.z)
  posScratch.ok = true
  return posScratch
}

const bubbleState = { list: [] as Bubble[], lastSpoke: new Map<string, number>() }
const arcs: Arc[] = []
const bursts: Burst[] = []
const dummy = new Object3D()
const viewDir = new Vector3()
const camRight = new Vector3()

const BubbleView = memo(function BubbleView({ bubble, text, kind }: { bubble: Bubble; text: string; kind: BubbleKind }) {
  const style = bubbleStyle(kind)
  const setGroup = useCallback(
    (g: Group | null) => {
      bubble.group = g
    },
    [bubble],
  )
  const onSync = useCallback(
    (t: TroikaText) => {
      bubble.troika = t
      const bb = t.textRenderInfo?.blockBounds
      if (bb) {
        bubble.w = bb[2] - bb[0]
        bubble.h = bb[3] - bb[1]
      }
    },
    [bubble],
  )
  return (
    <group ref={setGroup} visible={false}>
      <Text
        font={FONT_URL}
        fontSize={style.fontSize}
        maxWidth={style.maxWidth}
        lineHeight={BUBBLE_LINE_HEIGHT}
        textAlign="center"
        anchorX="center"
        anchorY="middle"
        color={style.ink}
        fillOpacity={0}
        outlineWidth={0}
        onSync={onSync}
        renderOrder={12}
      >
        {text}
      </Text>
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
        const p = agentPos(hoveredId, ctx)
        g.visible = p.ok && label !== ''
        if (p.ok) g.position.set(p.x, p.y + 2.95, p.z)
      } else if (gadgetPos) {
        g.visible = label !== ''
        g.position.set(gadgetPos.x, ctx.heightAt(gadgetPos.x, gadgetPos.z) + gadgetPos.h + 0.6, gadgetPos.z)
      } else g.visible = false
    }
    return registerSystem(SYS_EFFECTS + 1, system)
  }, [hoveredId, gadgetPos, label])
  if (!label) return null
  return (
    <group ref={ref} visible={false}>
      <Billboard follow>
        <Text font={FONT_URL} fontSize={0.42} anchorX="center" anchorY="bottom" color="#ffffff" outlineWidth={0.04} outlineColor="#060911">
          {label}
        </Text>
      </Billboard>
    </group>
  )
})

/** Rounded-rectangle backdrop, one instance per visible bubble, per-instance alpha. */
function makeBackdrop(capacity: number): { mesh: InstancedMesh; alpha: InstancedBufferAttribute; tint: InstancedBufferAttribute } {
  const w = 1
  const h = 1
  const r = 0.16
  const shape = new Shape()
  shape.moveTo(-w / 2 + r, -h / 2)
  shape.lineTo(w / 2 - r, -h / 2)
  shape.quadraticCurveTo(w / 2, -h / 2, w / 2, -h / 2 + r)
  shape.lineTo(w / 2, h / 2 - r)
  shape.quadraticCurveTo(w / 2, h / 2, w / 2 - r, h / 2)
  shape.lineTo(-w / 2 + r, h / 2)
  shape.quadraticCurveTo(-w / 2, h / 2, -w / 2, h / 2 - r)
  shape.lineTo(-w / 2, -h / 2 + r)
  shape.quadraticCurveTo(-w / 2, -h / 2, -w / 2 + r, -h / 2)
  const geom = new ShapeGeometry(shape, 6)
  const alpha = new InstancedBufferAttribute(new Float32Array(capacity), 1)
  alpha.setUsage(DynamicDrawUsage)
  geom.setAttribute('aAlpha', alpha)
  const tint = new InstancedBufferAttribute(new Float32Array(capacity * 3), 3)
  tint.setUsage(DynamicDrawUsage)
  geom.setAttribute('aTint', tint)
  const mat = new ShaderMaterial({
    transparent: true,
    depthWrite: false,
    vertexShader: /* glsl */ `
      attribute float aAlpha; attribute vec3 aTint; varying float vAlpha; varying vec3 vTint;
      void main() {
        vAlpha = aAlpha;
        vTint = aTint;
        gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */ `
      varying float vAlpha; varying vec3 vTint;
      void main() { gl_FragColor = vec4(vTint, vAlpha); }`,
  })
  const mesh = new InstancedMesh(geom, mat, capacity)
  mesh.instanceMatrix.setUsage(DynamicDrawUsage)
  mesh.frustumCulled = false
  mesh.count = 0
  mesh.renderOrder = 11
  return { mesh, alpha, tint }
}

export function Effects() {
  const [bubbles, setBubbles] = useState<Bubble[]>([])
  const ringRef = useRef<Mesh>(null)

  const ring = useMemo(() => {
    const g = new RingGeometry(0.95, 1.15, 40)
    g.rotateX(-Math.PI / 2)
    const m = new MeshBasicMaterial({ color: '#dfe7ff', transparent: true, opacity: 0.85, depthWrite: false })
    return { g, m }
  }, [])

  const backdrop = useMemo(() => makeBackdrop(MAX_VISIBLE_BUBBLES), [])

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

  useEffect(
    () => () => {
      ring.g.dispose()
      ring.m.dispose()
      backdrop.mesh.geometry.dispose()
      ;(backdrop.mesh.material as ShaderMaterial).dispose()
      arcObj.geom.dispose()
      pointsObj.geom.dispose()
    },
    [ring, backdrop, arcObj, pointsObj],
  )

  useEffect(() => {
    let dirtyBubbles = false
    const c = new Color()

    const addBurst = (x: number, z: number, y: number, hex: string, spread: number, lift: number, gravity: number, dur: number, now: number) => {
      c.set(hex)
      if (bursts.length >= MAX_BURSTS) bursts.shift()
      bursts.push({ x, y, z, t0: now, dur, r: c.r, g: c.g, b: c.b, spread, lift, gravity })
    }

    /** One bubble per (agent, kind): refresh it, or add one; true when the rendered set must change. */
    const upsertBubble = (agentId: string, kind: BubbleKind, text: string, ttlMs: number, now: number, lied: boolean): boolean => {
      const mine = bubbleState.list.find((b) => b.agentId === agentId && b.kind === kind)
      if (mine) {
        mine.born = now
        mine.ttlMs = ttlMs
        const changed = mine.text !== text || mine.lied !== lied
        if (changed) {
          mine.text = text
          mine.lied = lied
          mine.rev++
          mine.w = mine.h = 0
        }
        return changed
      }
      bubbleState.list.push({ agentId, kind, lied, ttlMs, text, born: now, rev: 0, group: null, troika: null, w: 0, h: 0, dist: 0, x: 0, y: 0, z: 0, ok: false })
      return true
    }

    const fire = (e: EventMsg, ctx: FrameCtx) => {
      const { now } = ctx
      if (isEventKind(e, 'talk')) {
        const speaker = e.payload.speaker_id
        const p = agentPos(speaker, ctx)
        if (!p.ok) return
        // scripted brains repeat themselves every tick: one bubble per speaker, 1.5 s cooldown
        const last = bubbleState.lastSpoke.get(speaker)
        if (last !== undefined && now - last < SPEAK_COOLDOWN_MS / 1000) return
        bubbleState.lastSpoke.set(speaker, now)
        const lied = Number(e.payload.claims_false ?? 0) > 0
        if (upsertBubble(speaker, 'speech', clampBubbleText(e.payload.text), BUBBLE_MS, now, lied)) dirtyBubbles = true
        return
      }
      if (e.kind === 'remember' || e.kind === 'revise_self' || e.kind === 'maintenance') {
        const p = e.payload as Record<string, unknown>
        const id = (typeof p.agent_id === 'string' && p.agent_id) || e.agent_id
        if (!id) return
        let text: string
        let ttl = NOTE_MS
        if (e.kind === 'remember') text = clampNoteText(p.title)
        else if (e.kind === 'revise_self') text = clampNoteText('self')
        else {
          const n = Number(p.commands ?? p.ok ?? 0)
          text = `✎ tidying memory (${Number.isFinite(n) ? n : 0} ${n === 1 ? 'change' : 'changes'})`
          ttl = MAINTENANCE_MS
        }
        if (upsertBubble(id, 'note', text, ttl, now, false)) dirtyBubbles = true
        return
      }
      if (e.kind === 'claim_feedback') {
        const p = e.payload as Record<string, unknown>
        const id = (typeof p.listener_id === 'string' && p.listener_id) || e.agent_id
        if (id && upsertBubble(id, 'mark', '✗', MARK_MS, now, false)) dirtyBubbles = true
        return
      }
      if (isEventKind(e, 'transfer')) {
        const a = agentPos(e.payload.from, ctx)
        const ax = a.x
        const az = a.z
        const aok = a.ok
        const b = agentPos(e.payload.to, ctx)
        if (!aok || !b.ok) return
        if (arcs.length >= MAX_ARCS) arcs.shift()
        arcs.push({ from: e.payload.from, to: e.payload.to, x0: ax, z0: az, x1: b.x, z1: b.z, t0: now })
        const s = history.agentIndex.get(e.payload.to)
        if (s !== undefined) perSlot.flashUntil[s] = now + ARC_MS / 1000 + 0.4
        return
      }
      if (isEventKind(e, 'windfall')) {
        const p = agentPos(e.payload.agent_id, ctx)
        if (!p.ok) return
        addBurst(p.x, p.z, p.y + 0.9, '#ffd166', 1.1, 2.6, 3.2, 1.6, now)
        const s = history.agentIndex.get(e.payload.agent_id)
        if (s !== undefined) perSlot.flashUntil[s] = now + 1.2
        return
      }
      if (isEventKind(e, 'birth')) {
        const p = agentPos(e.payload.agent_id, ctx)
        const s = history.agentIndex.get(e.payload.agent_id)
        const tier = mirror.rosterById.get(e.payload.agent_id)?.tier
        const hex = (tier && mirror.tierColor.get(tier)?.getHexString()) || '4fd1c5'
        if (p.ok) addBurst(p.x, p.z, p.y + 0.4, '#' + hex, 1.6, 1.4, 1.5, 1.4, now)
        if (s !== undefined) perSlot.flashUntil[s] = now + 1.0
        return
      }
      if (isEventKind(e, 'death')) {
        const p = agentPos(e.payload.agent_id, ctx)
        if (p.ok) addBurst(p.x, p.z, p.y + 0.8, '#6b7280', 0.7, -0.6, -0.8, 1.2, now)
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
        const p = agentPos(e.payload.agent_id, ctx)
        if (p.ok) addBurst(p.x, p.z, p.y + 0.5, '#2fbf8f', 0.35, 1.2, 1.8, 0.8, now)
        return
      }
      if (isEventKind(e, 'gadget_verified')) {
        const g = useStore.getState().gadgets?.items.find((i) => i.id === e.payload.gadget_id)
        if (g) addBurst(g.x, g.y, ctx.heightAt(g.x, g.y) + 0.8, '#8ce99a', 1.3, 1.6, 1.2, 1.5, now)
      }
    }

    const system = (ctx: FrameCtx) => {
      const { out, now, camera } = ctx
      fxQueue.drain(out.tick, (e) => fire(e, ctx))

      // --- bubbles: expire, locate, pick the nearest few within range, position + fade
      const list = bubbleState.list
      for (let i = list.length - 1; i >= 0; i--) {
        const b = list[i]!
        if ((now - b.born) * 1000 > b.ttlMs) {
          list.splice(i, 1)
          dirtyBubbles = true
          continue
        }
        const p = agentPos(b.agentId, ctx)
        b.ok = p.ok
        if (!p.ok || !camera) {
          b.dist = Infinity
          continue
        }
        const st = bubbleStyle(b.kind, b.lied)
        b.x = p.x
        b.y = p.y + st.lift
        b.z = p.z
        if (b.kind === 'mark') {
          camRight.set(1, 0, 0).applyQuaternion(camera.quaternion)
          b.x += camRight.x * MARK_SIDE
          b.z += camRight.z * MARK_SIDE
        }
        const dx = camera.position.x - b.x
        const dy = camera.position.y - b.y
        const dz = camera.position.z - b.z
        b.dist = Math.sqrt(dx * dx + dy * dy + dz * dz)
        if (b.dist > BUBBLE_MAX_DIST) b.dist = Infinity
      }
      // selection: the MAX_VISIBLE nearest (no allocation: repeated min scan over a tiny list)
      for (const b of list) if (b.group) b.group.visible = false
      let shown = 0
      const alphaArr = backdrop.alpha.array as Float32Array
      const tintArr = backdrop.tint.array as Float32Array
      if (camera) {
        while (shown < MAX_VISIBLE_BUBBLES) {
          let best: Bubble | null = null
          for (const b of list) {
            if (b.dist === Infinity || !b.group || b.group.visible) continue
            if (!best || b.dist < best.dist) best = b
          }
          if (!best) break
          const b = best
          const st = bubbleStyle(b.kind, b.lied)
          const age = (now - b.born) * 1000
          const fin = Math.min(1, age / FADE_MS)
          const fout = age > b.ttlMs - FADE_MS ? (b.ttlMs - age) / FADE_MS : 1
          const o = Math.max(0, Math.min(fin, fout))
          const g = b.group!
          g.visible = true
          g.position.set(b.x, b.y + Math.min(1, age / 600) * 0.12, b.z)
          g.quaternion.copy(camera.quaternion)
          // a mark pops in: overshoot then settle (no bounce under reduced motion)
          const pop = b.kind === 'mark' && !ctx.reducedMotion ? 0.6 + 0.4 * Math.min(1, age / 180) + 0.25 * Math.max(0, 1 - Math.abs(age - 180) / 160) : 1
          g.scale.setScalar(pop)
          if (b.troika) b.troika.fillOpacity = o
          // backdrop: same billboard, slightly behind the text along the view ray, sized to the measured block
          viewDir.set(b.x - camera.position.x, g.position.y - camera.position.y, b.z - camera.position.z).normalize()
          dummy.position.copy(g.position).addScaledVector(viewDir, 0.04)
          dummy.quaternion.copy(camera.quaternion)
          const w = ((b.w > 0 ? b.w : st.maxWidth) + BUBBLE_PAD * 2) * pop
          const h = ((b.h > 0 ? b.h : st.fontSize * BUBBLE_LINE_HEIGHT) + BUBBLE_PAD * 1.6) * pop
          dummy.scale.set(w, h, 1)
          dummy.updateMatrix()
          backdrop.mesh.setMatrixAt(shown, dummy.matrix)
          alphaArr[shown] = o * st.bgAlpha
          tintArr[shown * 3] = st.bg[0]
          tintArr[shown * 3 + 1] = st.bg[1]
          tintArr[shown * 3 + 2] = st.bg[2]
          shown++
        }
      }
      backdrop.mesh.count = shown
      if (shown > 0) {
        backdrop.mesh.instanceMatrix.needsUpdate = true
        backdrop.alpha.needsUpdate = true
        backdrop.tint.needsUpdate = true
      }
      if (dirtyBubbles) {
        dirtyBubbles = false
        setBubbles(list.slice())
      }

      // --- selection ring
      const ringMesh = ringRef.current
      if (ringMesh) {
        const sel = mirror.selectedId
        const p = sel ? agentPos(sel, ctx) : null
        ringMesh.visible = !!(p && p.ok)
        if (p && p.ok) {
          ringMesh.position.set(p.x, p.y + 0.06, p.z)
          const k = 1 + 0.06 * Math.sin(now * 3)
          ringMesh.scale.set(k, 1, k)
        }
      }

      // --- transfer arcs
      const ap = arcObj.pos.array as Float32Array
      let v = 0
      for (let i = arcs.length - 1; i >= 0; i--) {
        const a = arcs[i]!
        const u = ((now - a.t0) * 1000) / ARC_MS
        if (u > 1.2) {
          arcs.splice(i, 1)
          continue
        }
        const pa = agentPos(a.from, ctx)
        const x0 = pa.ok ? pa.x : a.x0
        const z0 = pa.ok ? pa.z : a.z0
        const y0 = (pa.ok ? pa.y : ctx.heightAt(a.x0, a.z0)) + 1.2
        const pb = agentPos(a.to, ctx)
        const x1 = pb.ok ? pb.x : a.x1
        const z1 = pb.ok ? pb.z : a.z1
        const y1 = (pb.ok ? pb.y : ctx.heightAt(a.x1, a.z1)) + 1.2
        const head = Math.min(1, u)
        const tail = Math.max(0, u - 0.3)
        const h = 1.2 + Math.hypot(x1 - x0, z1 - z0) * 0.25
        for (let s = 0; s < ARC_SEGS; s++) {
          const t0 = tail + ((head - tail) * s) / ARC_SEGS
          const t1 = tail + ((head - tail) * (s + 1)) / ARC_SEGS
          ap[v++] = x0 + (x1 - x0) * t0
          ap[v++] = y0 + (y1 - y0) * t0 + Math.sin(t0 * Math.PI) * h
          ap[v++] = z0 + (z1 - z0) * t0
          ap[v++] = x0 + (x1 - x0) * t1
          ap[v++] = y0 + (y1 - y0) * t1 + Math.sin(t1 * Math.PI) * h
          ap[v++] = z0 + (z1 - z0) * t1
        }
      }
      arcObj.geom.setDrawRange(0, v / 3)
      arcObj.lines.visible = v > 0
      if (v > 0) arcObj.pos.needsUpdate = true

      // --- particle bursts
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
  }, [arcObj, pointsObj, backdrop])

  return (
    <group>
      {bubbles.map((b) => (
        <BubbleView key={`${b.agentId}:${b.kind}`} bubble={b} text={b.text} kind={b.kind} />
      ))}
      <primitive object={backdrop.mesh} />
      <HoverTag />
      <mesh ref={ringRef} geometry={ring.g} material={ring.m} visible={false} />
      <primitive object={arcObj.lines} />
      <primitive object={pointsObj.points} />
    </group>
  )
}
