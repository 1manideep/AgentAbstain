#!/usr/bin/env node
/**
 * Behaviour screenshot: the fixture world at a daytime cluster with a few behaviours triggered at once, so a
 * still shows what normally flickers past: a note card and a writing gesture (remember), a nightly-maintenance
 * card, a talk with the speaker and listener turned to each other, and a listener who just found out it was
 * lied to (red X, head shake). The events are injected at the current render tick through window.__void.inject,
 * exactly as the socket would deliver them; everything else is the recorded fixture.
 *
 *   VITE_FEED=fixture npm run dev -- --port 5173      (in another shell)
 *   node scripts/screenshot-behaviour.mjs              (writes web/screenshots/behaviour-world.png)
 *
 * VOID_URL overrides the server. The browser is Playwright's own Chromium (`npx playwright install chromium`)
 * unless VOID_CHROME names an executable.
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'screenshots')
mkdirSync(outDir, { recursive: true })
const worldUrl = (process.env.VOID_URL ?? 'http://127.0.0.1:5173/').replace(/\/?$/, '/')
// Playwright's own Chromium (`npx playwright install chromium`; honours PLAYWRIGHT_BROWSERS_PATH) unless
// VOID_CHROME names a Chrome/Chromium executable to use instead.
const chrome = process.env.VOID_CHROME || undefined
const launchFailed = (e) => {
  const why = String(e?.message ?? e).split('\n')[0]
  console.error(`could not launch Chromium${chrome ? ` (VOID_CHROME=${chrome})` : ''}: ${why}`)
  console.error('hint: run npx playwright install chromium, or set VOID_CHROME to a Chrome/Chromium executable')
  process.exit(1)
}

const browser = await chromium.launch({
  headless: true,
  ...(chrome ? { executablePath: chrome } : {}),
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--disable-gpu-vsync'],
}).catch(launchFailed)
const errors = []
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 })
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(`error: ${m.text()}`)
})
const ready = async () => page.waitForFunction(() => Boolean(window.__void) && window.__void.history.count > 20, null, { timeout: 40000 })

let runId = 'none'
try {
  runId = JSON.parse(readFileSync(resolve(here, '..', 'public', 'fixtures', 'mock.jsonl'), 'utf8').split('\n')[0]).run_id ?? runId
} catch {
  /* default key */
}

for (let attempt = 0; ; attempt++) {
  try {
    await page.goto(`${worldUrl}?rig=1`, { waitUntil: 'load' })
    break
  } catch (e) {
    if (attempt > 40) throw e
    await new Promise((r) => setTimeout(r, 500))
  }
}
await ready()
await page.waitForTimeout(1500)

// pick the tightest group of awake agents, preferring daylight and three or more; fall back to pairs, then any hour
const pick = await page.evaluate((runId) => {
  const h = window.__void.history
  let best = null
  for (let i = 0; i < h.count; i++) {
    const f = h.frameAt(i)
    const day = f.tickOfDay >= 8 && f.tickOfDay <= 17
    const nodes = []
    for (let n = 0; n < f.nodeCapacity; n++) if (f.nodeIds[n]) nodes.push({ x: f.nodes[n * 5], y: f.nodes[n * 5 + 1] })
    const agents = []
    for (let s = 0; s < f.capacity; s++) {
      if (!f.ids[s] || f.flags[s * 4 + 1] !== 1 || f.flags[s * 4] === 1) continue // alive, awake
      agents.push({ id: f.ids[s], x: f.data[s * 6], y: f.data[s * 6 + 1] })
    }
    for (const a of agents) {
      const near = (b) => Math.hypot(a.x - b.x, a.y - b.y)
      const group = agents.filter((b) => near(b) < 7).sort((p, q) => near(p) - near(q)).slice(0, 4)
      if (group.length < 2) continue
      const spread = group.reduce((s, g) => s + near(g), 0) / group.length
      const score = (day ? 100 : 0) + Math.min(group.length, 4) * 10 - spread - i * 0.001
      const cx = group.reduce((s, g) => s + g.x, 0) / group.length
      const cz = group.reduce((s, g) => s + g.y, 0) / group.length
      let node = null
      let dn = Infinity
      for (const nd of nodes) {
        const d = Math.hypot(nd.x - cx, nd.y - cz)
        if (d < dn) [node, dn] = [nd, d]
      }
      const clear = Math.min(dn, 6) // standing clear of a crystal node reads better
      const total = score + clear
      if (!best || total > best.score) best = { index: i, tick: f.tick, tickOfDay: f.tickOfDay, score: total, ids: group.map((g) => g.id), cx, cz, node }

    }
  }
  if (!best) return null
  // the camera looks at the group from the side away from the nearest node, so the crystal sits behind them
  let dx = 0.55
  let dz = 0.83
  if (best.node) {
    const len = Math.hypot(best.cx - best.node.x, best.cz - best.node.y)
    if (len > 1e-6) [dx, dz] = [(best.cx - best.node.x) / len, (best.cz - best.node.y) / len]
  }
  const cam = { p: [best.cx + dx * 8.5 - dz * 2, 4.4, best.cz + dz * 8.5 + dx * 2], t: [best.cx, 1.9, best.cz] }
  localStorage.setItem(`void.cam.${runId}`, JSON.stringify(cam))
  return best
}, runId)
if (!pick) {
  console.error('no pair of awake agents found in the fixture')
  process.exit(1)
}
console.log('behaviour', JSON.stringify(pick))
await page.reload({ waitUntil: 'load' })
await ready()
await page.evaluate((index) => {
  const h = window.__void.history
  h.scrubToIndex(Math.min(index, h.count - 1))
  h.setSpeed(0)
}, pick.index)
await page.waitForTimeout(2500)

// inject the behaviours at the render tick
await page.evaluate(({ ids, tick }) => {
  const [a, b, c = a, d] = ids
  let seq = 9_000_000
  const ev = (kind, agent_id, payload) => ({ type: 'event', seq: seq++, tick, day: 1, kind, agent_id, visibility: 'public', payload })
  const inject = window.__void.inject
  inject(ev('talk', a, { speaker_id: a, listener_id: b, text: 'The node to the north is rich today. Go before the drought.', claims_made: 1, claims_false: 1 }))
  inject(ev('claim_feedback', b, { speaker_id: a, listener_id: b, claim: { subject: 'node', id: 'n2', attr: 'stock_bucket', value: 'rich' } }))
  inject(ev('remember', c, { agent_id: c, note_id: 'note_x', title: 'Node n2 at (8, -3) was thin on day 2', channel: 'observed' }))
  if (d) inject(ev('maintenance', d, { agent_id: d, commands: 3, ok: 3, refused: 0, strategy: 'index_builder' }))
}, { ids: pick.ids, tick: pick.tick })
await page.waitForTimeout(900)
const file = resolve(outDir, 'behaviour-world.png')
await page.screenshot({ path: file })
console.log('saved', file)
const errs = errors.filter((e) => !/DevTools|source map|favicon|THREE.Clock/i.test(e))
console.log(`console errors: ${errs.length}`)
for (const e of errs.slice(0, 10)) console.log('  ', e.slice(0, 300))
await browser.close()
if (errs.some((e) => e.startsWith('pageerror'))) process.exit(1)
