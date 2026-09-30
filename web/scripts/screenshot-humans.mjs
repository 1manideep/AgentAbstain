#!/usr/bin/env node
/**
 * Character screenshots with headless Chromium (SwiftShader):
 *
 *   humans-contact.png  the ten presets in a row, idle, frozen (gallery, `#/characters`)
 *   humans-moves.png    one preset seven times, one per clip, each frozen mid-clip
 *   humans-world.png    a character on the terrain in fixture mode, daytime close-up
 *                       (`?rig=1` forces characters on under a software rasteriser)
 *   humans-world-night.png  a group of walkers in the open (the fixture's agents only
 *                       leave the nodes in the first ticks, which are night frames)
 *
 *   npm run build && npx vite preview --port 4173        (gallery shots, in another shell)
 *   VITE_FEED=fixture npm run dev -- --port 5173         (world shot, in another shell)
 *   npm run screenshot:humans                             (writes web/screenshots/)
 *
 * VOID_PREVIEW_URL / VOID_URL override the two servers; VOID_MOVES_PRESET picks
 * the preset index for the moves strip (default 1).
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'screenshots')
mkdirSync(outDir, { recursive: true })

const previewUrl = (process.env.VOID_PREVIEW_URL ?? 'http://127.0.0.1:4173/').replace(/\/?$/, '/')
const worldUrl = (process.env.VOID_URL ?? 'http://127.0.0.1:5173/').replace(/\/?$/, '/')
const movesPreset = process.env.VOID_MOVES_PRESET ?? '1'
const executablePath = process.env.VOID_CHROME ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
const only = new Set((process.env.VOID_SHOTS ?? 'contact,moves,world,world-night').split(','))

const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--disable-gpu-vsync'],
})
const errors = []
const newPage = async (viewport = { width: 1600, height: 1000 }) => {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`error: ${m.text()}`)
  })
  return page
}
const gotoRetry = async (page, url) => {
  for (let attempt = 0; ; attempt++) {
    try {
      await page.goto(url, { waitUntil: 'load' })
      return
    } catch (e) {
      if (attempt > 40) throw e
      await new Promise((r) => setTimeout(r, 500))
    }
  }
}

if (only.has('contact')) {
  const page = await newPage({ width: 2200, height: 900 })
  await gotoRetry(page, `${previewUrl}?still=1&clip=idle&t=0.4#/characters`)
  await page.waitForTimeout(4000) // geometry, fonts (troika worker) and a few frames
  await page.screenshot({ path: resolve(outDir, 'humans-contact.png') })
  console.log('saved', resolve(outDir, 'humans-contact.png'))
  await page.close()
}

if (only.has('moves')) {
  const page = await newPage({ width: 2200, height: 900 })
  await gotoRetry(page, `${previewUrl}?moves=1&preset=${movesPreset}#/characters`)
  await page.waitForTimeout(4000)
  await page.screenshot({ path: resolve(outDir, 'humans-moves.png') })
  console.log('saved', resolve(outDir, 'humans-moves.png'))
  await page.close()
}

/**
 * World shot: scrub to a frame, pick a view, save the camera for the run
 * (`void.cam.<runId>` in localStorage), reload so CameraRig restores it, shoot.
 * `mode` 'day' prefers a daytime agent with neighbours (they cluster at nodes);
 * 'open' prefers a group of awake agents standing clear of the node crystals.
 */
const worldShot = async (file, mode) => {
  let runId = 'none'
  try {
    const first = readFileSync(resolve(here, '..', 'public', 'fixtures', 'mock.jsonl'), 'utf8').split('\n')[0]
    runId = JSON.parse(first).run_id ?? runId
  } catch {
    /* default key */
  }
  const page = await newPage()
  await gotoRetry(page, `${worldUrl}?rig=1`)
  await page.waitForFunction(() => Boolean(window.__void) && window.__void.history.count > 20, null, { timeout: 40000 })
  await page.waitForTimeout(2000)
  const view = await page.evaluate(
    ({ runId, mode }) => {
      const h = window.__void.history
      let best = null
      for (let i = 0; i < h.count; i++) {
        const f = h.frameAt(i)
        const day = f.tickOfDay >= 8 && f.tickOfDay <= 17
        if (mode === 'day' && !day) continue
        const nodes = []
        for (let n = 0; n < f.nodeCapacity; n++) if (f.nodeIds[n]) nodes.push({ x: f.nodes[n * 5], y: f.nodes[n * 5 + 1] })
        const awake = []
        for (let s = 0; s < f.capacity; s++) {
          if (!f.ids[s] || f.flags[s * 4 + 1] !== 1 || f.flags[s * 4] === 1) continue // alive, awake
          const x = f.data[s * 6]
          const y = f.data[s * 6 + 1]
          let dn = Infinity
          for (const nd of nodes) dn = Math.min(dn, Math.hypot(nd.x - x, nd.y - y))
          awake.push({ x, y, dn, heading: f.data[s * 6 + 2], walking: f.flags[s * 4 + 3] === 1 })
        }
        const pool = mode === 'open' ? awake.filter((a) => a.dn >= 1.5) : awake
        for (const a of pool) {
          const group = pool.filter((b) => Math.hypot(a.x - b.x, a.y - b.y) < 7)
          const score = mode === 'open' ? 2 * group.length + Math.min(a.dn, 6) + (day ? 3 : 0) : 1.5 * group.length + Math.min(a.dn, 8) + (a.walking ? 1 : 0)
          if (!best || score > best.score) {
            const cx = mode === 'open' ? group.reduce((acc, b) => acc + b.x, 0) / group.length : a.x
            const cz = mode === 'open' ? group.reduce((acc, b) => acc + b.y, 0) / group.length : a.y
            best = { index: i, tick: f.tick, tickOfDay: f.tickOfDay, score, group: group.length, dn: a.dn, cx, cz, heading: a.heading }
          }
        }
      }
      if (!best) best = { index: Math.min(h.count - 1, 10), tick: 0, tickOfDay: 0, score: 0, group: 0, dn: 0, cx: 0, cz: 0, heading: 0 }
      h.scrubToIndex(best.index)
      h.setSpeed(0)
      let cam
      if (mode === 'open') {
        const k = best.group > 1 ? 1.45 : 1
        cam = { p: [best.cx + 3.2 * k, 2.4 * k + 0.4, best.cz + 5.8 * k], t: [best.cx, 0.9, best.cz] }
      } else {
        // stand in front of the agent (heading 0 = +x, world y is scene z), a little to its left, sun side
        const fx = Math.cos(best.heading)
        const fz = Math.sin(best.heading)
        const px = best.cx + fx * 5.6 - fz * 2.2
        const pz = best.cz + fz * 5.6 + fx * 2.2
        cam = { p: [px, 2.7, pz], t: [best.cx, 0.9, best.cz] }
      }
      localStorage.setItem(`void.cam.${runId}`, JSON.stringify(cam))
      return { ...best, cam }
    },
    { runId, mode },
  )
  console.log(`world[${mode}]`, JSON.stringify(view))
  await page.reload({ waitUntil: 'load' })
  await page.waitForFunction(() => Boolean(window.__void) && window.__void.history.count > 20, null, { timeout: 40000 })
  await page.evaluate((index) => {
    const h = window.__void.history
    h.scrubToIndex(Math.min(index, h.count - 1))
    h.setSpeed(0)
  }, view.index)
  await page.waitForTimeout(3500) // terrain, characters and eased lighting settle
  const stats = await page.evaluate(() => window.__void.stats())
  console.log(`world[${mode}] drawCalls=${stats.calls} triangles=${stats.triangles} dpr=${stats.dpr}`)
  await page.screenshot({ path: resolve(outDir, file) })
  console.log('saved', resolve(outDir, file))
  await page.close()
}

if (only.has('world')) await worldShot('humans-world.png', 'day')
if (only.has('world-night')) await worldShot('humans-world-night.png', 'open')

const errs = errors.filter((e) => !/DevTools|source map|favicon|THREE.Clock/i.test(e))
console.log(`console errors: ${errs.length}`)
for (const e of errs.slice(0, 20)) console.log('  ', e.slice(0, 300))
await browser.close()
if (errs.some((e) => e.startsWith('pageerror'))) process.exit(1)
