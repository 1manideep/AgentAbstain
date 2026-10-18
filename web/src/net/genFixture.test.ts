/**
 * scripts/gen-fixture.mjs writes its synthetic fixture to a fresh OS temp dir by default and refuses
 * web/public/fixtures (the committed, genuine `void mock-feed` recording) unless given --force. Its tiers
 * carry placeholder model names: no model identifier from configs/base.yaml appears in the script or its output.
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'

const WEB = resolve(import.meta.dirname, '..', '..')
const SCRIPT = join(WEB, 'scripts', 'gen-fixture.mjs')
const COMMITTED = join(WEB, 'public', 'fixtures')
const BASE_YAML = resolve(WEB, '..', 'configs', 'base.yaml')

const scratch = mkdtempSync(join(tmpdir(), 'gen-fixture-test-'))
afterAll(() => rmSync(scratch, { recursive: true, force: true }))

function gen(args: string[], env: Record<string, string> = {}) {
  // TMPDIR (TMP/TEMP on Windows) points os.tmpdir() into this test's scratch dir, so the default run stays contained
  const tmp = mkdtempSync(join(scratch, 'os-tmp-'))
  const r = spawnSync(process.execPath, [SCRIPT, ...args], {
    cwd: WEB,
    encoding: 'utf8',
    env: { ...process.env, FIXTURE_OUT: '', TMPDIR: tmp, TMP: tmp, TEMP: tmp, ...env },
  })
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, tmp }
}

function snapshot(dir: string): Record<string, string> {
  return Object.fromEntries(readdirSync(dir).sort().map((f) => [f, readFileSync(join(dir, f), 'utf8')]))
}

function realModelIds(): string[] {
  const ids = [...readFileSync(BASE_YAML, 'utf8').matchAll(/^\s*model:\s*["']?([^\s"'#]+)/gm)].map((m) => m[1]!)
  return [...new Set(ids)].filter((id) => !id.startsWith('scripted'))
}

describe('gen-fixture.mjs', () => {
  it('writes to a fresh temp dir by default, never into public/fixtures', () => {
    const before = snapshot(COMMITTED)
    const r = gen([])
    expect(r.status, r.stderr).toBe(0)
    const out = /to (\S+)[\\/]mock\.jsonl/.exec(r.stdout)?.[1]
    expect(out, r.stdout).toBeTruthy()
    expect(relative(r.tmp, out!).startsWith('..')).toBe(false)
    expect(existsSync(join(out!, 'mock.jsonl'))).toBe(true)
    expect(existsSync(join(out!, 'api.json'))).toBe(true)
    expect(snapshot(COMMITTED)).toEqual(before)
  })

  it('refuses public/fixtures, or a directory under it, without --force', () => {
    const before = snapshot(COMMITTED)
    for (const target of [COMMITTED, join(COMMITTED, 'sub'), 'public/fixtures', './public/fixtures/']) {
      const r = gen(['--out', target])
      expect(r.status, `${target}: ${r.stdout}`).toBe(2)
      expect(r.stderr).toContain('--force')
    }
    const viaEnv = gen([], { FIXTURE_OUT: join(COMMITTED, 'sub') })
    expect(viaEnv.status).toBe(2)
    expect(existsSync(join(COMMITTED, 'sub'))).toBe(false)
    expect(snapshot(COMMITTED)).toEqual(before)
  })

  it('rejects --out without a directory', () => {
    const r = gen(['--out'])
    expect(r.status).toBe(2)
    expect(r.stderr).toContain('usage')
  })

  it('is deterministic and carries placeholder model names only', () => {
    const a = join(scratch, 'a')
    const b = join(scratch, 'b')
    expect(gen(['--out', a]).status).toBe(0)
    expect(gen(['--out', b]).status).toBe(0)
    expect(snapshot(a)).toEqual(snapshot(b))
    const hello = JSON.parse(readFileSync(join(a, 'mock.jsonl'), 'utf8').split('\n')[0]!) as {
      type: string
      config: { tiers: Record<string, { model: string }> }
    }
    expect(hello.type).toBe('hello')
    expect(Object.values(hello.config.tiers).map((t) => t.model).sort()).toEqual(['budget-model', 'frontier-model'])
    const ids = realModelIds()
    expect(ids.length).toBeGreaterThan(0)
    const texts = { script: readFileSync(SCRIPT, 'utf8'), ...snapshot(a) }
    for (const [name, text] of Object.entries(texts)) expect(ids.filter((id) => text.includes(id)), name).toEqual([])
  })
})

describe('the committed fixture', () => {
  it('is the genuine demo_memory recording, with api.json from the same run', () => {
    const lines = readFileSync(join(COMMITTED, 'mock.jsonl'), 'utf8').split('\n').filter((l) => l.trim())
    const hello = JSON.parse(lines[0]!) as { type: string; run_id: string; config: { name: string; memory_layer?: { enabled: boolean } } }
    expect(hello.type).toBe('hello')
    expect(hello.config.name).toBe('mock_feed')
    expect(hello.config.memory_layer?.enabled).toBe(true)
    const api = JSON.parse(readFileSync(join(COMMITTED, 'api.json'), 'utf8')) as { run_id: string; agents: Record<string, unknown> }
    expect(api.run_id).toBe(hello.run_id)
    const roster = new Set<string>()
    for (const l of lines) {
      const m = JSON.parse(l) as { type: string; agents?: Array<{ id: string }> }
      if (m.type === 'roster') for (const a of m.agents ?? []) roster.add(a.id)
    }
    expect(Object.keys(api.agents).sort()).toEqual([...roster].sort())
  })
})
