#!/usr/bin/env node
/**
 * Loads the app in fixture mode with headless Chromium, records console errors,
 * measures the frame rate (requestAnimationFrame count over 3 s) and the
 * renderer's draw calls, and saves two screenshots at ~6 s and ~12 s.
 *
 *   VITE_FEED=fixture npm run dev -- --port 5173   (in another shell)
 *   npm run screenshot                              (writes web/screenshots/)
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'screenshots')
mkdirSync(outDir, { recursive: true })

const url = process.env.VOID_URL ?? 'http://127.0.0.1:5173/'
const executablePath = process.env.VOID_CHROME ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'

const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--disable-gpu-vsync'],
})
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
const errors = []
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => {
  if (m.type() === 'error' || m.type() === 'warning') errors.push(`${m.type()}: ${m.text()}`)
})

// wait for the dev server
for (let attempt = 0; ; attempt++) {
  try {
    await page.goto(url, { waitUntil: 'load' })
    break
  } catch (e) {
    if (attempt > 40) throw e
    await new Promise((r) => setTimeout(r, 500))
  }
}
const t0 = Date.now()
await page.waitForFunction(() => Boolean(window.__void) && window.__void.history.count > 0, null, { timeout: 30000 })

const measure = async (label) => {
  const r = await page.evaluate(
    () =>
      new Promise((res) => {
        let n = 0
        const start = performance.now()
        const tick = () => {
          n++
          if (performance.now() - start < 3000) requestAnimationFrame(tick)
          else res({ fps: n / ((performance.now() - start) / 1000), ...window.__void.stats(), tick: window.__void.history.renderTick(), frames: window.__void.history.count })
        }
        requestAnimationFrame(tick)
      }),
  )
  console.log(`[${label}] fps=${r.fps.toFixed(1)} internalFps=${r.fps ? r.fps.toFixed(1) : '?'} drawCalls=${r.calls} triangles=${r.triangles} dpr=${r.dpr} renderTick=${r.tick} frames=${r.frames}`)
  return r
}

// first screenshot at ~6 s of playback
const wait = async (ms) => {
  const target = t0 + ms
  const d = target - Date.now()
  if (d > 0) await page.waitForTimeout(d)
}
await wait(3000)
const m1 = await measure('t≈6s')
await page.screenshot({ path: resolve(outDir, 'fixture-1.png') })
console.log('saved', resolve(outDir, 'fixture-1.png'))

// select an agent so the agent panel is populated in the second shot
await page.evaluate(() => {
  const h = window.__void.history
  const ids = Array.from(h.agentIndex.keys())
  if (ids.length) window.__void.select(ids[1] ?? ids[0])
})
await wait(9000)
const m2 = await measure('t≈12s')
await page.screenshot({ path: resolve(outDir, 'fixture-2.png') })
console.log('saved', resolve(outDir, 'fixture-2.png'))

const errs = errors.filter((e) => !/DevTools|source map|favicon/i.test(e))
console.log(`console errors/warnings: ${errs.length}`)
for (const e of errs.slice(0, 20)) console.log('  ', e.slice(0, 300))
console.log(JSON.stringify({ fps1: +m1.fps.toFixed(1), fps2: +m2.fps.toFixed(1), calls1: m1.calls, calls2: m2.calls, dpr: m2.dpr }))
await browser.close()
if (errs.some((e) => e.startsWith('pageerror'))) process.exit(1)
