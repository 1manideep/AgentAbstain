#!/usr/bin/env node
/**
 * Landscape screenshots: loads the app in fixture mode, scrubs the render clock
 * to a dawn frame (tick_of_day ≈ 6) and a night frame (tick_of_day ≈ 22) from
 * the connect burst, measures fps / draw calls at each, and writes
 * web/screenshots/landscape-1.png (dawn) and landscape-2.png (night).
 *
 *   VITE_FEED=fixture npm run dev -- --port 5173   (in another shell)
 *   npm run screenshot:landscape
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
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
})
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
const errors = []
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(`${m.type()}: ${m.text()}`)
})
for (let attempt = 0; ; attempt++) {
  try {
    await page.goto(url, { waitUntil: 'load' })
    break
  } catch (e) {
    if (attempt > 40) throw e
    await new Promise((r) => setTimeout(r, 500))
  }
}
await page.waitForFunction(() => Boolean(window.__void) && window.__void.history.count > 20, null, { timeout: 40000 })
await page.waitForTimeout(2500) // terrain + props build

const measure = (label) =>
  page.evaluate(
    (label) =>
      new Promise((res) => {
        let n = 0
        const start = performance.now()
        const tick = () => {
          n++
          if (performance.now() - start < 3000) requestAnimationFrame(tick)
          else {
            const s = window.__void.stats()
            res({ label, fps: +(n / ((performance.now() - start) / 1000)).toFixed(1), calls: s.calls, triangles: s.triangles, dpr: s.dpr })
          }
        }
        requestAnimationFrame(tick)
      }),
    label,
  )

/** Scrub to the first buffered frame whose tick_of_day matches, pause there. */
const scrubToHour = (tod) =>
  page.evaluate((tod) => {
    const h = window.__void.history
    let best = -1
    for (let i = 0; i < h.count; i++) {
      const f = h.frameAt(i)
      if (f.tickOfDay === tod) {
        best = i
        break
      }
    }
    if (best < 0) best = Math.min(h.count - 1, tod)
    h.scrubToIndex(best)
    h.setSpeed(0)
    return h.frameAt(best).tick
  }, tod)

const shots = [
  { file: 'landscape-1.png', tod: 7, label: 'dawn' },
  { file: 'landscape-2.png', tod: 22, label: 'night' },
]
const results = []
for (const s of shots) {
  const tick = await scrubToHour(s.tod)
  await page.waitForTimeout(2200) // eased lighting settles
  const m = await measure(s.label)
  await page.screenshot({ path: resolve(outDir, s.file) })
  console.log(`[${s.label} tick ${tick}] fps=${m.fps} drawCalls=${m.calls} triangles=${m.triangles} dpr=${m.dpr} → ${s.file}`)
  results.push({ ...m, tick })
}
const errs = errors.filter((e) => !/DevTools|source map|favicon|THREE.Clock/i.test(e))
console.log(`console errors: ${errs.length}`)
for (const e of errs.slice(0, 20)) console.log('  ', e.slice(0, 300))
console.log(JSON.stringify(results))
await browser.close()
if (errs.some((e) => e.startsWith('pageerror'))) process.exit(1)
