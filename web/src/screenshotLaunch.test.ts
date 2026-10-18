// The screenshot scripts (web/scripts/screenshot-*.mjs) on a browser that cannot start: they must exit 1 and
// print Playwright's whole launch error (the cause comes after its first line) followed by the install hints.
// A fake browser stands in for one that fails with a missing system library; nothing reaches a page, so no
// screenshot is written.
import { spawnSync } from 'node:child_process'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const scriptsDir = fileURLToPath(new URL('../scripts/', import.meta.url))
const SCRIPTS = ['screenshot-landscape.mjs', 'screenshot-humans.mjs', 'screenshot-behaviour.mjs']
const CAUSE = 'error while loading shared libraries: libnss3.so'
const HINT = 'run npx playwright install chromium, or set VOID_CHROME'

let scratch = ''
let fakeChrome = ''

const run = (script: string, env: Record<string, string | undefined>) => {
  const merged: Record<string, string | undefined> = { ...process.env, VOID_URL: 'http://127.0.0.1:9/', ...env }
  for (const k of Object.keys(merged)) if (merged[k] === undefined) delete merged[k]
  return spawnSync(process.execPath, [join(scriptsDir, script)], { env: merged, encoding: 'utf8', timeout: 60_000 })
}

describe.skipIf(process.platform === 'win32')('screenshot scripts on a browser that cannot start', () => {
  beforeAll(() => {
    scratch = mkdtempSync(join(tmpdir(), 'void-shot-'))
    fakeChrome = join(scratch, 'chrome')
    writeFileSync(fakeChrome, `#!/bin/sh\necho "chrome: ${CAUSE}: cannot open shared object file" >&2\nexit 127\n`)
    chmodSync(fakeChrome, 0o755)
  })
  afterAll(() => {
    if (scratch) rmSync(scratch, { recursive: true, force: true })
  })

  it.each(SCRIPTS)('%s prints the browser stderr and both hints', (script) => {
    const r = run(script, { VOID_CHROME: fakeChrome })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain(`could not launch Chromium (VOID_CHROME=${fakeChrome}):`)
    expect(r.stderr).toContain(CAUSE)
    expect(r.stderr).toContain(HINT)
    expect(r.stderr).toContain('npx playwright install-deps chromium')
    // The cause is printed before the hints.
    expect(r.stderr.indexOf(CAUSE)).toBeLessThan(r.stderr.indexOf(HINT))
  }, 60_000)

  it("falls back to Playwright's own browser when VOID_CHROME is unset", () => {
    // An empty browsers directory: Playwright resolves its own Chromium there and finds none.
    const browsers = mkdtempSync(join(scratch, 'browsers-'))
    const r = run('screenshot-landscape.mjs', { VOID_CHROME: undefined, PLAYWRIGHT_BROWSERS_PATH: browsers })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('could not launch Chromium:')
    expect(r.stderr).toContain(browsers)
    expect(r.stderr).toContain(HINT)
  }, 60_000)
})
