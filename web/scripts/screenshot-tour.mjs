#!/usr/bin/env node
/**
 * Visual tour of the fixture world: establishing shots across one day, a timeline strip, the control-room
 * cards, the ten characters close up with the inspector open, groups, lifecycle events and the memory layer.
 * Writes web/screenshots/tour/NN-name.png (1600x1000) and web/screenshots/tour/README.md (one caption each).
 *
 *   VITE_FEED=fixture VITE_FIXTURE_BURST=1000 npm run dev -- --port 5173   (in another shell)
 *   npm run screenshot:tour                                                 (about 15 minutes under SwiftShader)
 *
 * VITE_FIXTURE_BURST=1000 hands the page the whole recording at once; with the default burst each of the two
 * pages first waits for the fixture to stream in (about two and a half minutes).
 *
 * How a still is made: the script reads the recording itself to choose a tick and the agents to show, scrubs
 * the render clock to that frame and pauses, then frames the camera through the R3F root's orbit controls.
 * Candidate views (24 azimuths x a few elevations) are raycast against the live scene: rays from each subject's
 * body toward the camera must not hit a crystal, tree, rock, gadget, hill or another character, and rays
 * through the lower part of the picture must not stop on something close to the lens, and bubbles must not
 * overlap. Agents are placed where the page draws them (the history moves foragers off the node centre).
 * Short-lived effects (speech bubbles, note cards, the red X, bursts) are the recorded events of that tick
 * fed again through window.__void (the same path the socket takes); they live for 1.6-3 s of wall time and
 * SwiftShader renders about a frame a second, so the render loop is frozen on the first frame that shows
 * them fully opaque and the still is taken from that frame. Selection, follow and the feed filter go through
 * the app's store, imported from the dev server's module graph (`/src/state/store.ts`); that is why the
 * tour runs against `vite` and not a production build.
 *
 * VOID_URL overrides the server; VOID_TOUR=01,07,22 limits the shots by file-name prefix (README.md is then
 * left alone).
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'screenshots', 'tour')
mkdirSync(outDir, { recursive: true })
const worldUrl = (process.env.VOID_URL ?? 'http://127.0.0.1:5173/').replace(/\/?$/, '/')
const executablePath = process.env.VOID_CHROME ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
const only = process.env.VOID_TOUR ? process.env.VOID_TOUR.split(',').map((s) => s.trim()).filter(Boolean) : null
const wanted = (file) => !only || only.some((p) => file.startsWith(p))
/** Whether any shot numbered lo..hi is wanted (a page with none is not opened). */
const wantedRange = (lo, hi) => !only || only.some((p) => Number(p.slice(0, 2)) >= lo && Number(p.slice(0, 2)) <= hi)
const num = (n) => String(n).padStart(2, '0')

// ------------------------------------------------------------------ the recording

const fixture = readFileSync(resolve(here, '..', 'public', 'fixtures', 'mock.jsonl'), 'utf8')
  .split('\n')
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l))
const hello = fixture.find((m) => m.type === 'hello')
const runId = hello?.run_id ?? 'none'
const ticksPerDay = hello?.config?.ticks_per_day ?? 24
const names = new Map()
for (const m of fixture) if (m.type === 'roster') for (const a of m.agents) names.set(a.id, a.name)
const lastRoster = fixture.findLast((m) => m.type === 'roster')?.agents ?? []
const nameOf = (id) => names.get(id) ?? id
const idOf = (name) => [...names].find(([, n]) => n === name)?.[0] ?? null
const tierOf = (id) => lastRoster.find((a) => a.id === id)?.tier ?? '?'
const events = fixture.filter((m) => m.type === 'event')
const snapshots = fixture.filter((m) => m.type === 'snapshot')
const snapAt = new Map(snapshots.map((s) => [s.tick, s]))
const evAt = (tick, pred = () => true) => events.filter((e) => e.tick === tick && pred(e))
const todOf = (tick) => snapAt.get(tick)?.tick_of_day ?? -1
const dayOf = (tick) => snapAt.get(tick)?.day ?? 0
const hhmm = (tick) => `${num(Math.floor((todOf(tick) / ticksPerDay) * 24))}:00`
const when = (tick) => `day ${dayOf(tick)}, tick ${tick}, ${hhmm(tick)}`
const tickAt = (day, tod) => snapshots.find((s) => s.day === day && s.tick_of_day === tod)?.tick ?? null
const alive = (a) => a.status === 'alive'
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y)
const nodeDist = (snap, p) => Math.min(...snap.nodes.map((n) => dist(n, p)))
const finalGadgets = fixture.findLast((m) => m.type === 'gadgets')?.items ?? []
/** Distance to the nearest gadget pedestal (they stand from day 1, so an agent can be drawn inside one). */
const gadgetDist = (p) => Math.min(Infinity, ...finalGadgets.map((g) => dist(g, p)))
const daylight = (tick) => todOf(tick) >= 8 && todOf(tick) <= 16
const list = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`)

/**
 * Where the page draws each agent, per tick (filled from the page once it has the recording). The history
 * moves agents that stand on a node centre out to a ring at the crystal's foot, so snapshot positions are not
 * where the characters stand.
 */
let rendered = {}

/** Snapshot agents at their drawn positions, with their distance to the nearest node centre. */
const agentsAt = (tick) => {
  const s = snapAt.get(tick)
  const pos = rendered[tick] ?? {}
  return (s?.agents ?? []).map((a) => {
    const p = pos[a.id]
    const b = p ? { ...a, x: p[0], y: p[1] } : { ...a }
    return { ...b, dn: nodeDist(s, b), gd: gadgetDist(b) }
  })
}

/** Bubble boxes (world units, from bubbleStyle/bubbleText) for the framing's overlap test. */
const SPEECH = { lift: 2.9, w: 2.0, h: 1.25 }
const NOTE = { lift: 3.55, w: 1.7, h: 0.85 }
const bubblesOf = (agents, style) => agents.map((a) => ({ x: a.x, z: a.y, ...style }))
/** Not drawn inside or against a gadget pedestal. */
const clear = (a) => a.gd > 1.1

/** Frame points for agents: `top` is how much above the feet must stay in the picture; sleepers are cast low. */
const focusOf = (agents, top) => agents.map((a) => ({ id: a.id, x: a.x, z: a.y, top, rh: a.asleep ? [0.25, 0.45] : undefined }))

/**
 * Greedy group around each seed: members within `radius` of the seed and at least `gap` from every member
 * already taken (so their bubbles do not overlap). Returns the largest group by `weight` (default 1 each).
 */
function spacedGroup(pool, { radius, gap, weight = () => 1 }) {
  let best = { members: [], score: -1 }
  for (const seed of pool) {
    const members = [seed]
    const rest = pool.filter((b) => b !== seed && dist(seed, b) <= radius).sort((p, q) => dist(seed, p) - dist(seed, q))
    for (const b of rest) if (members.every((m) => dist(m, b) >= gap)) members.push(b)
    const score = members.reduce((s, m) => s + weight(m), 0)
    if (score > best.score) best = { members, score }
  }
  return best
}

// ------------------------------------------------------------------ browser

const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--disable-gpu-vsync'],
})
const errors = []
const captions = []

/**
 * In-page helpers, installed once per page as window.__tour. Runs in the browser: everything it uses comes
 * from window.__void or from the dev server's modules (the same module instances the app uses).
 */
async function installTour() {
  const urls = performance.getEntriesByType('resource').map((e) => e.name)
  const threeUrl = urls.find((n) => /\/\.vite\/deps\/three\.js(\?|$)/.test(n))
  const THREE = threeUrl ? await import(threeUrl) : null
  const { useStore } = await import('/src/state/store.ts')
  const { heightAt } = await import('/src/scene/terrain.ts')
  const { sunDirection } = await import('/src/scene/daycycle.ts')
  const v = window.__void
  const root = () => v.scene.__r3f.root.getState()
  let seq = 9_000_000
  let frozenAt = 0
  const fresh = (e) => ({ ...e, seq: seq++ })

  /**
   * Meshes that can hide an agent: terrain, trees, rocks, crystals and gadgets. Characters are tested as
   * cylinders at their frame positions instead (agentCylinders): the puppets' skinned meshes do not raycast
   * where they are drawn.
   */
  const obstacles = () => {
    const out = []
    const shown = (o) => {
      for (let p = o; p; p = p.parent) if (!p.visible) return false
      return true
    }
    v.scene.traverse((o) => {
      if (!o.isMesh || o.isSkinnedMesh || !shown(o)) return
      const mat = Array.isArray(o.material) ? o.material[0] : o.material
      const geo = o.geometry?.type ?? ''
      if (!mat || mat.type === 'ShaderMaterial' || mat.type === 'MeshBasicMaterial') return // sky, water, bubbles, decals
      if (geo === 'SphereGeometry' || geo === 'PlaneGeometry' || geo === 'TorusGeometry' || geo === 'RingGeometry' || geo === 'CircleGeometry') return
      if (o.isInstancedMesh && (o.count === 0 || o.count > 200)) return // grass tufts are too small to matter
      if (o.isInstancedMesh) o.boundingSphere = null // instances move (crystals grow with stock): recompute
      o.userData.tourTerrain = !o.isInstancedMesh && (o.geometry?.attributes?.position?.count ?? 0) > 5000
      out.push(o)
    })
    return out
  }

  /**
   * Solid stand-ins for things that move between framing and capture: every agent in the frame under the
   * render clock as an upright cylinder (low when asleep or dead), and every node's crystal cluster as a
   * cylinder around its spires (the cluster turns slowly, so its gaps are not where they were when framed).
   */
  const agentCylinders = () => {
    const h = v.history
    const f = h.frameAt(h.renderIndex())
    const out = []
    for (let s = 0; s < f.capacity; s++) {
      if (!f.ids[s] || f.flags[s * 4 + 1] === 0) continue
      const x = f.data[s * 6]
      const z = f.data[s * 6 + 1]
      const y0 = heightAt(x, z)
      const low = f.flags[s * 4] === 1 || f.flags[s * 4 + 1] !== 1
      out.push({ id: f.ids[s], agent: true, x, z, r: low ? 0.45 : 0.3, y0, y1: y0 + (low ? 0.55 : 1.85) })
    }
    for (let n = 0; n < f.nodeCapacity; n++) {
      if (!f.nodeIds[n]) continue
      const x = f.nodes[n * 5]
      const z = f.nodes[n * 5 + 1]
      const y0 = heightAt(x, z) - 0.1
      out.push({ id: `node:${f.nodeIds[n]}`, agent: false, x, z, r: 1.15, y0, y1: y0 + 3.6 })
    }
    return out
  }

  /**
   * Distance along a unit ray to the first agent cylinder in [near, far] (skipping `skip`), or -1. `halo`
   * widens and raises the cylinders to take in the tier ring that floats over each head.
   */
  const hitAgent = (cyl, o, d, near, far, skip, halo = false) => {
    let best = -1
    const a = d.x * d.x + d.z * d.z
    if (a < 1e-9) return -1
    for (const c of cyl) {
      if (c.id === skip) continue
      const ox = o.x - c.x
      const oz = o.z - c.z
      const b = 2 * (ox * d.x + oz * d.z)
      const r = halo && c.agent ? Math.max(c.r, 0.6) : c.r
      const disc = b * b - 4 * a * (ox * ox + oz * oz - r * r)
      if (disc < 0) continue
      const sq = Math.sqrt(disc)
      for (const t of [(-b - sq) / (2 * a), (-b + sq) / (2 * a)]) {
        if (t < near || t > far) continue
        const y = o.y + d.y * t
        if (y >= c.y0 && y <= c.y1 + (halo && c.agent ? 0.9 : 0) && (best < 0 || t < best)) best = t
      }
    }
    return best
  }

  window.__tour = {
    /**
     * Scrub the render clock to the frame of `tick` (+ `ahead` frames) and pause there. The newest frame with
     * that tick wins: a buffer that holds a replay twice keeps its lifecycle bookkeeping for the newer copy.
     */
    go(tick, ahead = 0) {
      const h = v.history
      let i = h.count - 1
      while (i >= 0 && h.frameAt(i).tick !== tick) i--
      if (i < 0) return null
      h.scrubToIndex(Math.min(h.count - 1, i + ahead))
      h.setSpeed(0)
      const f = h.frameAt(h.renderIndex())
      return { tick: f.tick, day: f.day, tod: f.tickOfDay }
    },

    setCam(p, t) {
      const st = root()
      st.camera.position.set(p[0], p[1], p[2])
      st.controls.target.set(t[0], t[1], t[2])
      st.controls.update()
    },

    select(id, follow = false) {
      const s = useStore.getState()
      s.select(id)
      s.follow(follow ? id : null)
    },

    /**
     * Play `list` (recorded events) and freeze the render loop on the first frame that shows their effects in
     * full: at least `texts` bubbles mounted and opaque, particles in flight when `burst` is set. Bubbles and
     * bursts live for 1.6-3 s of wall time, a re-fired bubble is reborn transparent, and SwiftShader draws about
     * a frame a second, so a timed screenshot catches them fading in or already gone. The events go to the
     * store's ring once (characters gesture from it) and are re-queued on the effects queue every frame until
     * the bubble texts are mounted; then the queue is left alone and the next frame with everything opaque is
     * the one kept on screen. `release()` restarts the loop after the screenshot.
     */
    capture({ list, texts = 0, burst = false, minFrames = 2, maxFrames = 16 }) {
      const gl = v.gl
      const prev = gl.render
      const frames = () => v.stats().frames
      const bubbles = () => {
        let synced = 0
        let opaque = 0
        v.scene.traverse((o) => {
          if (typeof o.text !== 'string' || !o.parent?.visible || !o.textRenderInfo) return
          synced++
          if (o.fillOpacity >= 0.8) opaque++
        })
        return { synced, opaque }
      }
      const flying = () => {
        let n = 0
        v.scene.traverse((o) => {
          if (o.isPoints && o.geometry.getAttribute('aColor')?.itemSize === 4) n += Number.isFinite(o.geometry.drawRange.count) ? o.geometry.drawRange.count : 0
        })
        return n > 0
      }
      for (const e of list) v.inject(fresh(e))
      const start = frames()
      let last = start
      let warm = texts > 0 || burst
      return new Promise((resolve) => {
        gl.render = function (...args) {
          prev.apply(this, args)
          const f = frames()
          if (f === last) return // the composer renders more than once per frame: look once
          last = f
          const b = bubbles()
          const done = f - start >= minFrames && b.opaque >= texts && (!burst || flying())
          if (done || f - start >= maxFrames) {
            gl.render = prev
            frozenAt = root().clock.elapsedTime
            root().setFrameloop('never')
            resolve({ ok: done, frames: f - start, ...b })
            return
          }
          if (warm && texts > 0 && b.synced >= texts) warm = false
          if (warm) for (const e of list) v.fx.push(fresh(e))
        }
      })
    },

    /** Restart the render loop; the clock resumes where it stopped (setFrameloop resets it to 0). */
    release() {
      const st = root()
      st.setFrameloop('always')
      st.clock.elapsedTime = frozenAt
    },

    /** Render at device pixel ratio 1 (the performance monitor drops SwiftShader to 0.75). */
    sharp() {
      root().setDpr(1)
    },

    /** Drawn position of every agent per tick, from the newest frame holding that tick. */
    positions() {
      const h = v.history
      const out = {}
      for (let i = 0; i < h.count; i++) {
        const f = h.frameAt(i)
        const m = {}
        for (let s = 0; s < f.capacity; s++) if (f.ids[s]) m[f.ids[s]] = [f.data[s * 6], f.data[s * 6 + 1]]
        out[f.tick] = m
      }
      return out
    },

    /** Reset the camera rig's 30 s idle-drift timer without touching the canvas. */
    touch() {
      window.dispatchEvent(new WheelEvent('wheel'))
    },

    /**
     * Pick a camera for `spec.focus` (agents that must be seen unobstructed) and `spec.extra` (points that only
     * have to be in the picture). Per candidate azimuth/elevation the distance grows from `minDist` until every
     * point (its feet and `top` above them) projects inside `margin` of the stage. Rays from each focus agent's
     * body (`rh` heights) toward the camera count blockers; rays through the lower picture count clutter close
     * to the lens. The score prefers an open view, the agent's front (`heading`), the lit side (`sun`), a
     * preferred azimuth and a short distance.
     */
    frame(spec) {
      const st = root()
      const aspect = st.size.width / st.size.height
      const focus = (spec.focus ?? []).map((p) => ({ id: p.id ?? null, x: p.x, z: p.z, g: heightAt(p.x, p.z), top: p.top ?? spec.top ?? 1.95, rh: p.rh ?? spec.rayHeights ?? [0.7, 1.15, 1.55] }))
      const extra = (spec.extra ?? []).map((p) => ({ x: p.x, z: p.z, g: heightAt(p.x, p.z), top: p.top ?? 0.6 }))
      const all = [...focus, ...extra]
      const cx = spec.center?.x ?? all.reduce((s, p) => s + p.x, 0) / all.length
      const cz = spec.center?.z ?? all.reduce((s, p) => s + p.z, 0) / all.length
      const ground = focus.length ? focus : all
      const gy = ground.reduce((s, p) => s + p.g, 0) / ground.length
      const target = [cx, gy + (spec.lift ?? 0.9), cz]
      const sun = { x: 0, y: 0, z: 0 }
      sunDirection(((spec.tod ?? 12) / (spec.ticksPerDay ?? 24)) * 24, sun)
      const sunLen = Math.hypot(sun.x, sun.z) || 1
      const margin = spec.margin ?? 0.8
      const minDist = spec.minDist ?? 6.3
      const maxDist = spec.maxDist ?? 45
      const els = spec.els ?? [0.3, 0.42]
      if (!THREE) {
        const az = spec.prefAz ?? spec.heading ?? Math.PI / 2
        const d = Math.max(minDist, spec.fallbackDist ?? 9)
        const el = els[0]
        return { p: [cx + d * Math.cos(el) * Math.cos(az), target[1] + d * Math.sin(el), cz + d * Math.cos(el) * Math.sin(az)], t: target, d, rays: 0 }
      }
      const tv = new THREE.Vector3(...target)
      const probe = new THREE.PerspectiveCamera(42, aspect, 0.5, 900)
      const q = new THREE.Vector3()
      const inView = (x, y, z) => {
        q.set(x, y, z).project(probe)
        return q.z < 1 && Math.abs(q.x) <= margin && Math.abs(q.y) <= margin
      }
      const obs = obstacles()
      const cyl = agentCylinders()
      const ray = new THREE.Raycaster()
      const from = new THREE.Vector3()
      const dir = new THREE.Vector3()
      const ndc = new THREE.Vector2()
      const right = new THREE.Vector3()
      let best = null
      for (const el of els) {
        for (let k = 0; k < 24; k++) {
          const az = (k / 24) * Math.PI * 2
          dir.set(Math.cos(el) * Math.cos(az), Math.sin(el), Math.cos(el) * Math.sin(az))
          let d = minDist
          let fits = false
          for (; d <= maxDist; d *= 1.06) {
            probe.position.copy(tv).addScaledVector(dir, d)
            probe.lookAt(tv)
            probe.updateMatrixWorld()
            if (all.every((p) => inView(p.x, p.g + 0.05, p.z) && inView(p.x, p.g + p.top, p.z))) {
              fits = true
              break
            }
          }
          if (!fits) continue
          const cp = probe.position.clone()
          if (cp.y < heightAt(cp.x, cp.z) + 1.0) continue
          let blocked = 0
          let rays = 0
          for (const p of focus) {
            for (const hgt of p.rh) {
              from.set(p.x, p.g + hgt, p.z)
              const len = from.distanceTo(cp)
              ray.set(from, q.copy(cp).sub(from).normalize())
              ray.near = 0.45 // past the agent's own body
              ray.far = len + 1.2 // and a little past the camera, so a canopy around it counts too
              rays++
              if (ray.intersectObjects(obs, false).length > 0 || hitAgent(cyl, from, ray.ray.direction, 0.3, len, p.id) >= 0) blocked++
            }
          }
          // clutter: something other than the ground within 85 % of the subject distance in the lower picture
          let clutter = 0
          let probes = 0
          for (const y of [-0.8, -0.45, -0.1]) {
            for (const x of [-0.85, -0.45, 0, 0.45, 0.85]) {
              ndc.set(x, y)
              ray.setFromCamera(ndc, probe)
              ray.near = 0.5
              ray.far = d * 0.85
              const hit = ray.intersectObjects(obs, false)[0]
              const agentAt = hitAgent(cyl, ray.ray.origin, ray.ray.direction, 0.5, d * 0.85, null, true)
              probes++
              if ((hit && !hit.object.userData.tourTerrain && (agentAt < 0 || hit.distance < agentAt)) || (agentAt >= 0 && (!hit || agentAt < hit.distance))) clutter++
            }
          }
          const facing = spec.heading == null ? 0 : Math.cos(az - spec.heading)
          const lit = spec.sun ? (Math.cos(az) * sun.x + Math.sin(az) * sun.z) / sunLen : 0
          const pref = spec.prefAz == null ? 0 : Math.cos(az - spec.prefAz)
          // bubbles (speech, note cards) that would overlap on screen
          let overlap = 0
          if (spec.bubbles?.length > 1) {
            right.setFromMatrixColumn(probe.matrixWorld, 0)
            const boxes = spec.bubbles.map((b) => {
              const y = heightAt(b.x, b.z) + b.lift
              const c = q.set(b.x, y, b.z).project(probe).clone()
              const e = q.set(b.x, y, b.z).addScaledVector(right, b.w / 2).project(probe).clone()
              const t = q.set(b.x, y + b.h / 2, b.z).project(probe).clone()
              return { x: c.x, y: c.y, hw: Math.abs(e.x - c.x), hh: Math.abs(t.y - c.y) }
            })
            for (let i = 0; i < boxes.length; i++) {
              for (let j = i + 1; j < boxes.length; j++) {
                const [m, n] = [boxes[i], boxes[j]]
                if (Math.abs(m.x - n.x) < m.hw + n.hw && Math.abs(m.y - n.y) < m.hh + n.hh) overlap++
              }
            }
          }
          const score = -12 * (rays ? blocked / rays : 0) - 4 * (clutter / probes) - 6 * overlap + 1.6 * facing + 1.0 * lit + (spec.prefWeight ?? 0.8) * pref - 0.03 * d
          if (!best || score > best.score) best = { p: cp.toArray(), t: target, blocked, rays, clutter, overlap, d, az, el, score }
        }
      }
      return best
    },
  }
  return { raycast: Boolean(THREE) }
}

async function openWorld(initialCam) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`error: ${m.text()}`)
  })
  // a saved camera skips the rig's initial framing animation; a large resource buffer keeps three's URL listed
  await page.addInitScript(
    ({ key, cam }) => {
      try {
        performance.setResourceTimingBufferSize(5000)
        localStorage.setItem(key, JSON.stringify(cam))
      } catch {
        /* storage unavailable: the rig frames the population instead */
      }
    },
    { key: `void.cam.${runId}`, cam: initialCam },
  )
  for (let attempt = 0; ; attempt++) {
    try {
      await page.goto(`${worldUrl}?rig=1`, { waitUntil: 'load' })
      break
    } catch (e) {
      if (attempt > 40) throw e
      await new Promise((r) => setTimeout(r, 500))
    }
  }
  await page.waitForFunction((n) => Boolean(window.__void) && window.__void.history.count >= n, snapshots.length, { timeout: 240000 })
  const info = await page.evaluate(installTour)
  if (!info.raycast) console.warn('three.js module not found: framing without occlusion checks')
  rendered = await page.evaluate(() => window.__tour.positions())
  await page.waitForTimeout(1500) // terrain, props, characters and fonts
  return page
}

const wait = (page, ms) => page.waitForTimeout(ms)
const tour = (page, fn, arg) => page.evaluate(fn, arg)

/**
 * Wait for `n` rendered frames (and at least 1.2 s). The scene eases straw grass, storm darkening and the follow
 * camera per frame with dt capped at 0.1 s, so under a software rasteriser running at about one frame a second a
 * wall-clock wait would catch them half way.
 */
async function settle(page, n) {
  const f0 = await page.evaluate(() => window.__void.stats().frames)
  await wait(page, 1200)
  await page.waitForFunction((target) => window.__void.stats().frames >= target, f0 + n, { timeout: 180000, polling: 200 })
}

/** Put the aside back at the top, or scroll it so the card whose title starts with `title` is at the top. */
const scrollRoom = (page, title) =>
  page.evaluate((title) => {
    const room = document.querySelector('.control-room')
    if (!room) return
    if (!title) {
      room.scrollTop = 0
      return
    }
    const card = [...room.querySelectorAll('.card')].find((c) => (c.querySelector('.card-title')?.textContent ?? '').startsWith(title))
    if (card) room.scrollTop = card.offsetTop - room.offsetTop - 10
  }, title ?? null)

/** Click a feed chip by its label. */
const chip = (page, label) => page.locator('.feed-filters .chip', { hasText: new RegExp(`^${label}$`) }).first().click()

/**
 * One still. `view` is a fixed {p, t} or a frame() spec. `play` events are played through capture() and the
 * still is taken on the frame that shows them (`texts` bubbles opaque, particles in flight with `burst`).
 */
async function shot(page, file, caption, { tick, ahead = 0, view, select = null, follow = false, play = [], texts = 0, burst = false, frames = 8, before } = {}) {
  if (!wanted(file)) return null
  const at = await tour(page, ({ tick, ahead }) => window.__tour.go(tick, ahead), { tick, ahead })
  await tour(page, ({ id, follow }) => window.__tour.select(id, follow), { id: select, follow })
  await tour(page, () => window.__tour.sharp())
  if (before) await before()
  await settle(page, 2) // characters stand at the new frame before rays are cast
  let cam = view
  const tod = at?.tod ?? 12
  if (view && !view.p) cam = await tour(page, (spec) => window.__tour.frame(spec), { tod, ticksPerDay, sun: tod >= 7 && tod <= 17, ...view })
  if (cam) await tour(page, ({ p, t }) => window.__tour.setCam(p, t), cam)
  await tour(page, () => window.__tour.touch())
  await settle(page, frames)
  if ((await page.evaluate(() => window.__void.stats().dpr)) < 1) {
    // the performance monitor lowered the pixel ratio again while the frames settled
    await tour(page, () => window.__tour.sharp())
    await settle(page, 2)
  }
  let fx = null
  if (play.length) {
    fx = await tour(page, (opts) => window.__tour.capture(opts), { list: play, texts, burst })
    if (!fx.ok) console.warn(`  ${file}: effects not fully on screen after ${fx.frames} frames (${fx.opaque}/${texts} bubbles)`)
  }
  await page.screenshot({ path: resolve(outDir, file) })
  if (fx) await tour(page, () => window.__tour.release())
  const dpr = await page.evaluate(() => window.__void.stats().dpr)
  const r1 = (v) => v.map((x) => +x.toFixed(1)).join(',')
  const how = cam?.rays !== undefined ? `, dist ${cam.d.toFixed(1)}, blocked ${cam.blocked}/${cam.rays}, clutter ${cam.clutter ?? 0}, overlap ${cam.overlap ?? 0}, cam ${r1(cam.p)} -> ${r1(cam.t)}` : ''
  const fxs = fx ? `, fx ${fx.ok ? 'ok' : 'partial'} in ${fx.frames} frames` : ''
  console.log(`saved ${file} (tick ${at?.tick}, dpr ${dpr}${how}${fxs})`)
  captions.push({ file, caption })
  return cam
}

// ------------------------------------------------------------------ page 1: the world at large, the control room

const WORLD = hello?.config?.world_size ?? 44
const startCam = { p: [-WORLD * 0.42, WORLD * 0.62, WORLD * 0.95], t: [0, 0, -2] }
const defaultAz = Math.atan2(0.62, -0.35) // the camera rig's own sun-side view direction
const nodes0 = snapshots[0].nodes
const establishDay = 2
const hours = [
  ['dawn', 7],
  ['noon', 12],
  ['dusk', 17],
  ['night', 22],
]

if (wantedRange(1, 15)) {
  const page = await openWorld(startCam)

  // establishing: every node basin in the picture from the rig's default direction, one camera for four hours
  const map = { extra: nodes0.map((n) => ({ x: n.x, z: n.y, top: 3 })), center: { x: 0, z: 0 }, minDist: 25, els: [0.62], prefAz: defaultAz, prefWeight: 4, sun: false, margin: 0.8 }
  let mapCam = null
  for (const [k, [label, tod]] of hours.entries()) {
    const tk = tickAt(establishDay, tod)
    const pop = agentsAt(tk).filter(alive)
    const sleeping = pop.filter((a) => a.asleep).length
    const extra = k === 0 ? ' Right: spend gauges, the empty inspector and the event feed on its default "story" filter.' : ' Same camera.'
    mapCam =
      (await shot(page, `${num(k + 1)}-establish-${label}.png`, `Whole map at ${label} (${when(tk)}): six crystal nodes in stone rings, ${pop.length} agents (${sleeping} asleep).${extra} Gadget pedestals (blue horns, yellow beacons) come from the recording's final gadget list.`, {
        tick: tk,
        view: mapCam ?? map,
        frames: 12,
      })) ?? mapCam
  }

  // timeline strip: one camera over the busiest stretch of nodes, noon of days 1, 3, 5, 7 and 10
  const stripTicks = [1, 3, 5, 7, 10].map((d) => tickAt(d, 12)).filter((x) => x !== null)
  const visits = nodes0
    .map((n) => ({ n, around: stripTicks.flatMap((tk) => agentsAt(tk).filter((a) => alive(a) && dist(a, n) < 7)) }))
    .sort((a, b) => b.around.length - a.around.length)
  const hubs = []
  for (const v of visits) if (hubs.length < 3 && hubs.every((h) => dist(h.n, v.n) < 23)) hubs.push(v)
  const strip = {
    extra: [...hubs.map((h) => ({ x: h.n.x, z: h.n.y, top: 3 })), ...hubs.flatMap((h) => h.around).map((a) => ({ x: a.x, z: a.y, top: 2 }))],
    minDist: 12,
    els: [0.55],
    prefAz: defaultAz,
    prefWeight: 1.5,
    sun: false,
    margin: 0.85,
  }
  let stripCam = null
  for (const [k, tk] of stripTicks.entries()) {
    const s = snapAt.get(tk)
    const epoch = s.scarcity < 1 ? ` Drought epoch: scarcity ${s.scarcity}, weather ${s.weather.toFixed(2)}.` : s.scarcity > 1 ? ` Boom epoch: scarcity ${s.scarcity}, weather ${s.weather.toFixed(2)}.` : ''
    const inView = agentsAt(tk).filter((a) => alive(a) && hubs.some((h) => dist(a, h.n) < 7))
    stripCam =
      (await shot(page, `${num(5 + k)}-timeline-day${num(dayOf(tk))}.png`, `Timeline ${k + 1}/5, noon of day ${dayOf(tk)} (tick ${tk}), same camera over nodes ${hubs.map((h) => h.n.id).join(', ')}: ${inView.length} agents here (${inView.filter((a) => a.asleep).length} asleep), ${agentsAt(tk).filter(alive).length} alive in the world.${epoch}`, {
        tick: tk,
        view: stripCam ?? strip,
        frames: 24,
      })) ?? stripCam
  }

  // control room: the cards below the feed (they show the end of the recording), the world at noon of day 7
  const roomTick = tickAt(7, 12)
  await shot(page, '10-room-wealth-teff.png', `Control room: wealth distribution of the living agents (tier colours, Gini) and the prompt-visible T_eff histogram with the tiers' collapse temperatures.`, {
    tick: roomTick,
    view: stripCam ?? strip,
    before: () => scrollRoom(page, 'Wealth'),
  })
  await shot(page, '11-room-degradation-population.png', `Control room: observed degradation vs stress, degeneration events per generation, and population by generation over the run.`, {
    tick: roomTick,
    view: stripCam ?? strip,
    before: () => scrollRoom(page, 'Observed degradation'),
  })
  await shot(page, '12-room-claims-gossip-epochs.png', `Control room: false-claim rate by tier, the gossip graph (who passed notes to whom) and the epochs strip (drought, arrival, boom).`, {
    tick: roomTick,
    view: mapCam ?? map,
    before: () => scrollRoom(page, 'False-claim'),
  })
  await shot(page, '13-room-chronicle-tasks-controls.png', `Control room: the latest chronicle entry, the task board and the operator controls card (collapsed by default, opened here).`, {
    tick: roomTick,
    view: mapCam ?? map,
    before: async () => {
      await page.locator('.card-head', { hasText: /^Controls/ }).first().click()
      await scrollRoom(page, 'Chronicle')
    },
  })
  const enzo = idOf('Enzo')
  await shot(page, '14-room-lineage.png', `Control room: the lineage tree, founders with their replacements and offspring nested under them; deaths struck through with their tick; Enzo selected (highlighted row).`, {
    tick: roomTick,
    select: enzo,
    view: mapCam ?? map,
    before: () => scrollRoom(page, 'Lineage'),
  })
  await tour(page, () => window.__tour.select(null))
  await scrollRoom(page, null)

  // nightly maintenance: midnight, the most well-spaced agents with a maintenance event, standing clear of crystals
  let night = null
  for (const s of snapshots) {
    if (s.tick_of_day !== 0) continue
    const ids = new Set(evAt(s.tick, (e) => e.kind === 'maintenance').map((e) => e.agent_id))
    const pool = agentsAt(s.tick).filter((a) => alive(a) && ids.has(a.id) && clear(a))
    const g = spacedGroup(pool, { radius: 7, gap: 2.2, weight: (a) => (a.asleep ? 1.3 : 1) })
    if (g.members.length && (!night || g.score > night.score)) night = { tick: s.tick, ...g }
  }
  if (night) {
    const m = evAt(night.tick, (e) => e.kind === 'maintenance' && night.members.some((a) => a.id === e.agent_id))
    await shot(page, '15-memory-maintenance-feed.png', `Nightly memory maintenance at ${when(night.tick)}: the recorded maintenance events of ${list(night.members.map((a) => nameOf(a.id)))} replayed as "tidying memory" cards. Right: the feed filtered to the memory kinds (remember, share_note, maintenance, memory_command, revise_self).`, {
      tick: night.tick,
      view: { focus: focusOf(night.members, 4.1), bubbles: bubblesOf(night.members, NOTE), sun: false, els: [0.3, 0.42], margin: 0.78, minDist: 8 },
      play: m,
      texts: Math.min(6, m.length),
      before: async () => {
        for (const k of ['remember', 'share_note', 'maintenance', 'memory_command', 'revise_self']) await chip(page, k)
      },
    })
  }
  await page.close()
}

// ------------------------------------------------------------------ page 2: characters, groups, events, memory

/** A daytime close-up frame for one of the founders: awake, not crowded, standing clear of the crystals. */
function portraitTick(id) {
  let best = null
  for (const s of snapshots) {
    const tod = s.tick_of_day
    if (tod < 9 || tod > 15) continue
    const a = agentsAt(s.tick).find((x) => x.id === id)
    if (!a || !alive(a) || a.asleep || a.degenerate) continue
    const crowd = agentsAt(s.tick).filter((b) => b.id !== id && alive(b) && dist(a, b) < 1.5).length
    const score = -3 * crowd + Math.min(a.dn, 4) + (clear(a) ? 2 : 0) - Math.abs(tod - 12) * 0.25 + (a.anim === 'walk' ? 0.5 : 0) - s.tick * 0.002
    if (!best || score > best.score) best = { tick: s.tick, score, a }
  }
  return best
}

if (wantedRange(16, 35)) {
  const page = await openWorld(startCam)
  const founders = ['Ada', 'Bao', 'Cyra', 'Dev', 'Enzo', 'Faye', 'Gil', 'Hex', 'Ivo', 'Juno']
  for (const [k, name] of founders.entries()) {
    const id = idOf(name)
    const pick = id ? portraitTick(id) : null
    if (!pick) continue
    const a = pick.a
    const fate = events.find((e) => e.kind === 'death' && e.agent_id === id)
    await shot(page, `${num(16 + k)}-character-${name.toLowerCase()}.png`, `${name} (${tierOf(id)}) close up at ${when(pick.tick)}, selected and followed; the inspector on the right shows the end-of-recording record${fate ? ` (${fate.payload.cause} death at tick ${fate.tick})` : ' (alive)'}.`, {
      tick: pick.tick,
      select: id,
      follow: true,
      view: { focus: [{ id: a.id, x: a.x, z: a.y }], heading: a.heading, minDist: 6.3, maxDist: 9, els: [0.2, 0.3] },
    })
  }

  // a conversation: the lit tick with the most speakers standing far enough apart for their bubbles
  let talk = null
  for (const s of snapshots) {
    if (s.tick_of_day < 7 || s.tick_of_day > 16) continue
    const lines = evAt(s.tick, (e) => e.kind === 'talk')
    if (lines.length < 2) continue
    const pos = new Map(agentsAt(s.tick).map((a) => [a.id, a]))
    const speakers = [...new Set(lines.map((e) => e.payload.speaker_id))].map((i) => pos.get(i)).filter((a) => a && !a.asleep && clear(a))
    const g = spacedGroup(speakers, { radius: 8, gap: 2.4 })
    const score = g.score + (daylight(s.tick) ? 0.5 : 0)
    if (g.members.length >= 2 && (!talk || score > talk.score)) talk = { tick: s.tick, members: g.members, lines, score }
  }
  if (talk) {
    const ids = new Set(talk.members.map((a) => a.id))
    const said = talk.members.map((a) => talk.lines.find((e) => e.payload.speaker_id === a.id))
    const listeners = said.map((e) => agentsAt(talk.tick).find((a) => a.id === e.payload.listener_id)).filter((a) => a && !ids.has(a.id) && dist(a, talk.members[0]) < 8)
    await shot(page, '26-group-conversation.png', `A conversation at ${when(talk.tick)}: the recorded talk lines of ${list(said.map((e) => `${nameOf(e.payload.speaker_id)} (to ${nameOf(e.payload.listener_id)})`))} replayed as speech bubbles.`, {
      tick: talk.tick,
      select: said[0].payload.speaker_id,
      view: { focus: [...focusOf(talk.members, 3.6), ...focusOf(listeners, 2)], bubbles: bubblesOf(talk.members, SPEECH), els: [0.22, 0.32], margin: 0.82 },
      play: said,
      texts: said.length,
    })
  }

  // foraging: the lit tick and node with the most foragers standing between the crystals rather than in them
  let forage = null
  for (const s of snapshots) {
    if (!daylight(s.tick)) continue
    const byNode = new Map()
    for (const e of evAt(s.tick, (x) => x.kind === 'forage')) byNode.set(e.payload.node_id, [...(byNode.get(e.payload.node_id) ?? []), e])
    for (const [node, fs] of byNode) {
      const n = s.nodes.find((x) => x.id === node)
      const who = agentsAt(s.tick).filter((a) => fs.some((e) => e.payload.agent_id === a.id))
      const score = who.length
      if (!forage || score > forage.score) forage = { tick: s.tick, node: n, who, fs, score }
    }
  }
  if (forage) {
    await shot(page, '27-group-foraging.png', `Foraging at node ${forage.node.id} (${when(forage.tick)}): ${list(forage.who.map((a) => nameOf(a.id)))} on the ring at the crystals' foot; the recorded forage events replayed (green bursts, node pulse, crouch).`, {
      tick: forage.tick,
      view: { focus: forage.who.map((a) => ({ id: a.id, x: a.x, z: a.y, top: 2, rh: [0.45, 0.85] })), extra: [{ x: forage.node.x, z: forage.node.y, top: 3.2 }], els: [0.35, 0.5], margin: 0.75, minDist: 7 },
      play: forage.fs,
      burst: true,
    })
  }

  // sleepers: the small hours with the most agents asleep near each other and clear of the crystals
  let sleep = null
  for (const s of snapshots) {
    // the first hour after sunrise: still asleep, and bright enough under a software rasteriser to see the poses
    if (s.tick_of_day !== 7) continue
    const pool = agentsAt(s.tick).filter((a) => alive(a) && a.asleep && clear(a))
    const g = spacedGroup(pool, { radius: 3.5, gap: 0.8 })
    if (!sleep || g.score > sleep.score) sleep = { tick: s.tick, ...g }
  }
  if (sleep) {
    await shot(page, '28-group-sleeping.png', `Still asleep after sunrise (${when(sleep.tick)}): ${list(sleep.members.map((a) => nameOf(a.id)))} curled on their sides. Night itself renders near black under SwiftShader (see 04).`, {
      tick: sleep.tick,
      view: { focus: focusOf(sleep.members, 1.0), els: [0.45, 0.6], sun: false, margin: 0.6, minDist: 6.3 },
    })
  }

  // a birth: the first offspring born in daylight, else the first daylight replacement
  const births = events.filter((e) => e.kind === 'birth' && todOf(e.tick) >= 7 && todOf(e.tick + 1) <= 16)
  const kidOf = (e) => agentsAt(e.tick + 1).find((a) => a.id === e.payload.agent_id)
  const birth = births.find((e) => e.payload.kind === 'offspring' && kidOf(e) && clear(kidOf(e))) ?? births.find((e) => kidOf(e) && clear(kidOf(e))) ?? births[0]
  if (birth) {
    const kid = kidOf(birth)
    const parent = agentsAt(birth.tick + 1).find((a) => a.id === birth.payload.parent_id)
    const near = [kid, ...(parent && alive(parent) && dist(parent, kid) < 6 ? [parent] : [])]
    const replaced = events.find((e) => e.kind === 'death' && e.tick === birth.tick && e.payload.replacement_id === birth.payload.agent_id)
    const body = replaced && agentsAt(birth.tick + 1).find((a) => a.id === replaced.agent_id)
    const scene = [near.length > 1 ? `next to ${nameOf(parent.id)}` : '', replaced ? `replacing ${replaced.payload.name}, who died this tick${body && dist(body, kid) < 6 ? ' and lies on the ground nearby' : ''}` : ''].filter(Boolean).join(', ')
    await shot(page, '29-event-birth.png', `Birth at ${when(birth.tick)}: ${birth.payload.name} (${birth.payload.kind}, parent ${nameOf(birth.payload.parent_id)}, generation ${birth.payload.generation}, ${birth.payload.tier}, endowed $${birth.payload.endowment_usd.toFixed(2)}) appears${scene ? `, ${scene}` : ''}; shown one tick later, newborn selected; the recorded birth event replayed (tier-coloured burst).`, {
      tick: birth.tick,
      ahead: 1, // births scale in over the 400 ms of virtual time before the first frame holding the newborn
      select: kid.id,
      view: { focus: focusOf(near, 2.4), heading: kid.heading, els: [0.25, 0.35] },
      play: [birth],
      burst: true,
    })
  }

  // a death: the first agent to die in daylight, shown on the frame where it lies dead
  const death = events.find((e) => e.kind === 'death' && daylight(e.tick)) ?? events.find((e) => e.kind === 'death')
  if (death) {
    const body = agentsAt(death.tick).find((a) => a.id === death.agent_id)
    await shot(page, '30-event-death.png', `Death at ${when(death.tick)}: ${death.payload.name} (${death.payload.tier}) goes ${body?.status ?? 'out'} (cause "${death.payload.cause}", estate $${death.payload.estate_usd.toFixed(3)}) and falls on its back; the recorded death event replayed (grey burst); the inspector shows the record.`, {
      tick: death.tick,
      select: death.agent_id,
      view: { focus: [{ id: body.id, x: body.x, z: body.y, top: 1.2, rh: [0.2, 0.45] }], els: [0.45, 0.6], minDist: 6.3, maxDist: 9 },
      play: [death],
      burst: true,
    })
  }

  // degeneration: a lit degeneration whose agent stands clear of others and of the crystals
  let degen = null
  for (const e of events) {
    if (e.kind !== 'degeneration' || !daylight(e.tick)) continue
    const here = agentsAt(e.tick)
    const a = here.find((x) => x.id === e.agent_id)
    if (!a || !alive(a) || a.asleep) continue
    const crowd = here.filter((b) => b.id !== a.id && alive(b) && dist(a, b) < 1.5).length
    const score = -crowd * 2 + Math.min(a.dn, 4) + (a.degenerate ? 2 : 0) + (clear(a) ? 2 : 0)
    if (!degen || score > degen.score) degen = { e, a, score }
  }
  if (degen) {
    const p = degen.e.payload
    await shot(page, '31-event-degeneration.png', `Degeneration at ${when(degen.e.tick)}: ${p.agent_name} (${p.tier}) degenerates (${p.mode}, T_eff ${p.t_eff.toFixed(2)} at or over T_c ${p.t_c.toFixed(2)}): arms out, twitching, flickering; the recorded event replayed.`, {
      tick: degen.e.tick,
      select: degen.a.id,
      follow: true,
      view: { focus: [{ id: degen.a.id, x: degen.a.x, z: degen.a.y }], heading: degen.a.heading, minDist: 6.3, maxDist: 9, els: [0.22, 0.32] },
      play: [degen.e],
    })
  }

  // a windfall in daylight
  const winds = events.filter((e) => e.kind === 'windfall' && todOf(e.tick) >= 7 && todOf(e.tick) <= 17)
  const awakeClear = (e) => {
    const a = agentsAt(e.tick).find((x) => x.id === e.payload.agent_id)
    return a && !a.asleep && clear(a)
  }
  const wind = winds.find(awakeClear) ?? winds[0]
  if (wind) {
    const a = agentsAt(wind.tick).find((x) => x.id === wind.payload.agent_id)
    await shot(page, '32-event-windfall.png', `Windfall at ${when(wind.tick)}: ${nameOf(a.id)}${a.asleep ? ', asleep,' : ''} receives $${wind.payload.amount_usd.toFixed(3)} from no recorded source; the recorded event replayed (gold burst).`, {
      tick: wind.tick,
      select: a.id,
      follow: true,
      view: { focus: [{ id: a.id, x: a.x, z: a.y, top: 3.2 }], heading: a.heading, minDist: 6.3, maxDist: 10, els: [0.25, 0.35] },
      play: [wind],
      burst: true,
    })
  }

  // a gadget: the first gadget verified in the light, with its pedestal and its owner
  const gadgets = finalGadgets
  const pedestalOf = (e) => gadgets.find((g) => g.id === e.payload.gadget_id)
  const clearance = (e) => (pedestalOf(e) ? nodeDist(snapAt.get(e.tick), pedestalOf(e)) : -1)
  const verified = events.filter((e) => e.kind === 'gadget_verified' && todOf(e.tick) >= 7 && todOf(e.tick) <= 17 && pedestalOf(e)).sort((a, b) => clearance(b) - clearance(a))[0]
  const item = verified && pedestalOf(verified)
  if (verified && item) {
    const owner = agentsAt(verified.tick).find((a) => a.id === verified.payload.owner)
    const withOwner = owner && alive(owner) && dist(owner, item) < 8
    const nearby = gadgets.filter((g) => Math.hypot(g.x - item.x, g.y - item.y) < 4.5)
    const at = withOwner ? (dist(owner, item) < 1.5 ? `, ${nameOf(owner.id)} at its pedestal` : `, ${nameOf(owner.id)} nearby`) : ''
    await shot(page, '33-event-gadget.png', `Gadget at ${when(verified.tick)}: ${verified.payload.name} (${item.render.shape}, effect ${verified.payload.effect?.kind ?? '?'} ${verified.payload.effect?.value ?? ''}) verified for ${nameOf(verified.payload.owner)}${at}; the recorded event replayed (burst at the pedestal).${nearby.length > 1 ? ` ${nearby.length - 1} more pedestal${nearby.length > 2 ? 's' : ''} in view.` : ''} Pedestals come from the recording's final gadget list.`, {
      tick: verified.tick,
      select: verified.payload.owner,
      view: {
        focus: withOwner ? [{ id: owner.id, x: owner.x, z: owner.y }] : [],
        extra: nearby.map((g) => ({ x: g.x, z: g.y, top: 2.4 })),
        els: [0.3, 0.4],
        minDist: 7,
        margin: 0.75,
      },
      play: [verified],
      burst: true,
    })
  }

  // remember: the lit tick with the most well-spaced writers clear of the crystals
  let rem = null
  for (const s of snapshots) {
    if (!daylight(s.tick)) continue
    const writers = new Set(evAt(s.tick, (e) => e.kind === 'remember').map((e) => e.agent_id))
    const pool = agentsAt(s.tick).filter((a) => alive(a) && !a.asleep && writers.has(a.id) && clear(a))
    const g = spacedGroup(pool, { radius: 7, gap: 2.2 })
    if (g.members.length && (!rem || g.score > rem.score)) rem = { tick: s.tick, ...g }
  }
  if (rem) {
    const notes = rem.members.map((a) => evAt(rem.tick, (e) => e.kind === 'remember' && e.agent_id === a.id)[0])
    await shot(page, '34-memory-remember.png', `Remember at ${when(rem.tick)}: the recorded remember events of ${list(rem.members.map((a) => nameOf(a.id)))} replayed as cream note cards carrying the note titles, with the writing gesture.`, {
      tick: rem.tick,
      select: rem.members[0].id,
      view: { focus: focusOf(rem.members, 4.1), bubbles: bubblesOf(rem.members, NOTE), els: [0.22, 0.32], margin: 0.8 },
      play: notes,
      texts: notes.length,
    })
  }

  // claim feedback: a lit listener who finds out the speaker lied, with the talk that carried the lie
  const pairs = events.filter((e) => e.kind === 'claim_feedback' && daylight(e.tick) && evAt(e.tick, (x) => x.kind === 'talk' && x.payload.speaker_id === e.payload.speaker_id && x.payload.listener_id === e.payload.listener_id).length)
  const pairScore = (e) => {
    const [sp, li] = [e.payload.speaker_id, e.payload.listener_id].map((id) => agentsAt(e.tick).find((a) => a.id === id))
    if (!sp || !li) return -Infinity
    return Math.min(sp.dn, 2) + Math.min(li.dn, 2) + (clear(sp) ? 1 : 0) + (clear(li) ? 1 : 0) - Math.abs(dist(sp, li) - 2.5) - (li.asleep ? 3 : 0)
  }
  const fb = pairs.sort((a, b) => pairScore(b) - pairScore(a))[0]
  if (fb) {
    const lines = evAt(fb.tick, (x) => x.kind === 'talk' && x.payload.speaker_id === fb.payload.speaker_id && x.payload.listener_id === fb.payload.listener_id).slice(0, 1)
    const fbs = evAt(fb.tick, (x) => x.kind === 'claim_feedback' && x.payload.listener_id === fb.payload.listener_id && x.payload.speaker_id === fb.payload.speaker_id)
    const pair = agentsAt(fb.tick).filter((a) => a.id === fb.payload.speaker_id || a.id === fb.payload.listener_id)
    const c = fb.payload.claim
    await shot(page, '35-memory-claim-feedback.png', `Claim feedback at ${when(fb.tick)}: ${nameOf(fb.payload.listener_id)} finds out ${nameOf(fb.payload.speaker_id)} lied ("${c.subject}${c.id ? ` ${c.id}` : ''} ${c.attr.replace(/_/g, ' ')} = ${c.value}"): the red X beside the listener on the side away from the speaker, and ${nameOf(fb.payload.speaker_id)}'s speech bubble; recorded talk and feedback replayed.`, {
      tick: fb.tick,
      select: fb.payload.listener_id,
      view: {
        focus: focusOf(pair, 3.6),
        bubbles: [...bubblesOf(pair.filter((a) => a.id === fb.payload.speaker_id), SPEECH), ...bubblesOf(pair.filter((a) => a.id === fb.payload.listener_id), { lift: 2.5, w: 1.6, h: 0.7 })],
        els: [0.22, 0.32],
        margin: 0.8,
      },
      play: [...lines, ...fbs],
      texts: 2,
    })
  }
  await page.close()
}

// ------------------------------------------------------------------ captions and errors

if (!only) {
  captions.sort((a, b) => a.file.localeCompare(b.file))
  const lines = [
    '# Visual tour',
    '',
    `Stills of the fixture world (\`public/fixtures/mock.jsonl\`, run \`${runId}\`, ${snapshots.length} ticks of ${ticksPerDay} per day) taken by \`npm run screenshot:tour\` (\`scripts/screenshot-tour.mjs\`) at 1600x1000 under headless Chromium with SwiftShader.`,
    '',
    'Events said to be "replayed" are the recorded events of that tick fed again through `window.__void.inject` and the effects queue, so that bubbles and bursts that live for a second or two are on screen when the still is taken; nothing is invented. The control room on the right always shows the end of the recording (the feed, the charts and the inspector are not scrubbed with the world). Gadget pedestals come from the final gadget list, so they stand from day 1.',
    '',
    '| file | caption |',
    '|---|---|',
    ...captions.map((c) => `| [${c.file}](${c.file}) | ${c.caption.replace(/\|/g, '\\|')} |`),
    '',
  ]
  writeFileSync(resolve(outDir, 'README.md'), lines.join('\n'))
  console.log('saved', resolve(outDir, 'README.md'))
}
const errs = errors.filter((e) => !/DevTools|source map|favicon|THREE.Clock/i.test(e))
console.log(`shots: ${captions.length}`)
console.log(`console errors: ${errs.length}`)
for (const e of errs.slice(0, 20)) console.log('  ', e.slice(0, 300))
await browser.close()
if (errs.some((e) => e.startsWith('pageerror'))) process.exit(1)
