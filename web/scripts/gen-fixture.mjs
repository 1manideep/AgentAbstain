#!/usr/bin/env node
/**
 * Generates web/public/fixtures/mock.jsonl (server → client messages, DESIGN §15)
 * and web/public/fixtures/api.json (the /api responses the control room fetches).
 *
 * 300 ticks, 8 agents on two tiers walking smooth-but-irregular paths in a 60×60
 * world, stress ramping and relaxing, one agent asleep mid-day, one death at
 * tick 180 with a replacement birth two ticks later, resource nodes with
 * oscillating stock, interleaved events, a metrics row per tick, a day row every
 * 24 ticks, and ts_ms advancing by irregular deltas (200 ms – 3 s) with one 20 s
 * pause gap. Fully deterministic: seeded PRNG, no wall clock.
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = resolve(here, '..', 'public', 'fixtures')
mkdirSync(outDir, { recursive: true })

// ------------------------------------------------------------ prng

function mulberry32(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const rand = mulberry32(20260930)
const pick = (arr) => arr[Math.floor(rand() * arr.length)]
const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v)
const round = (v, d = 3) => Math.round(v * 10 ** d) / 10 ** d
const usd = (v) => Math.round(v * 1e6) / 1e6

// ------------------------------------------------------------ config

const TICKS = 300
const TICKS_PER_DAY = 24
const WORLD = 60
const RUN_ID = 'fixture-run-0001'
const TS0 = 1_750_000_000_000
const TIERS = {
  frontier: { color: '#4fd1c5', model: 'claude-opus-5-5', provider: 'anthropic', t_c: 1.8, cost: 0.011 },
  budget: { color: '#f6ad55', model: 'claude-haiku-4-5', provider: 'anthropic', t_c: 1.78, cost: 0.004 },
}
const CONFIG = {
  world_size: WORLD,
  ticks_per_day: TICKS_PER_DAY,
  tick_seconds: 0,
  render_delay_ticks: 2,
  population_cap: 12,
  daily_cap_usd: 3.0,
  total_cap_usd: 30.0,
  tiers: Object.fromEntries(Object.entries(TIERS).map(([k, v]) => [k, { color: v.color, model: v.model, provider: v.provider }])),
  degeneration_mode: 'both',
}

const NAMES = ['Ada', 'Bao', 'Cyra', 'Dev', 'Enzo', 'Faye', 'Gil', 'Hana']
const TIER_OF = ['frontier', 'budget', 'frontier', 'budget', 'frontier', 'budget', 'frontier', 'budget']

const nodes = [
  { id: 'n1', x: 12, y: 14, capacity: 40, phase: 0.0 },
  { id: 'n2', x: 47, y: 11, capacity: 40, phase: 1.1 },
  { id: 'n3', x: 30, y: 31, capacity: 60, phase: 2.2 },
  { id: 'n4', x: 9, y: 46, capacity: 40, phase: 3.3 },
  { id: 'n5', x: 50, y: 49, capacity: 40, phase: 4.4 },
  { id: 'n6', x: 33, y: 52, capacity: 40, phase: 5.5 },
].map((n) => ({ ...n, stock: n.capacity * (0.5 + 0.4 * rand()), lastStock: 0 }))

// ------------------------------------------------------------ agents

function makeAgent(i, name, tier, generation, parent_id, born_tick, x, y) {
  return {
    id: `a${String(i).padStart(2, '0')}`,
    name,
    tier,
    generation,
    parent_id,
    born_tick,
    died_tick: null,
    status: 'alive',
    x,
    y,
    heading: rand() * Math.PI * 2,
    speed: 0,
    targetHeading: rand() * Math.PI * 2,
    stress: 0.12 + rand() * 0.1,
    stressOffset: rand() * 0.25,
    balance: 1.5,
    asleep: false,
    degenerate: false,
    anim: 'idle',
    last_action: { type: 'idle', target: null, ok: true },
    effects: [],
    mode: 'wander', // wander | forage | idle | sleep
    modeTicks: 0,
    forageNode: null,
    lastForage: 0,
    balanceSeries: [],
    selfVersions: [],
    notes: [],
    calls: [],
    slept: false,
  }
}

const agents = NAMES.map((n, i) =>
  makeAgent(i + 1, n, TIER_OF[i], 0, null, 0, 10 + rand() * 40, 10 + rand() * 40),
)
const byId = () => Object.fromEntries(agents.map((a) => [a.id, a]))
const alive = () => agents.filter((a) => a.status === 'alive')

// ------------------------------------------------------------ output

const lines = []
let seq = 0
const emit = (m) => lines.push(JSON.stringify(m))
const event = (tick, kind, agent_id, payload, visibility = 'public') => {
  seq++
  emit({ type: 'event', seq, tick, day: Math.floor(tick / TICKS_PER_DAY), kind, agent_id, visibility, payload })
}

const rosterMsg = () => ({
  type: 'roster',
  agents: agents.map((a) => ({
    id: a.id,
    name: a.name,
    tier: a.tier,
    generation: a.generation,
    parent_id: a.parent_id,
    born_tick: a.born_tick,
    died_tick: a.died_tick,
    status: a.status,
  })),
})

// gadgets ----------------------------------------------------------------
let gadgetsRev = 1
const gadgets = [
  { id: 'g01', name: 'stock-lens', owner: 'a01', x: 14, y: 17, render: { shape: 'cube', color: '#7ab8ff', scale: 0.9, label: 'stock lens', rotation_deg: 20, height_offset: 0 }, uses: 3 },
  { id: 'g02', name: 'rain-cover', owner: 'a04', x: 45, y: 14, render: { shape: 'cylinder', color: '#C2A8FF', scale: 1.1, label: 'rain cover', rotation_deg: 0, height_offset: 0 }, uses: 1 },
]
const gadgetsMsg = () => ({ type: 'gadgets', rev: gadgetsRev, items: gadgets.map((g) => ({ ...g })) })
const LATER_GADGETS = [
  { tick: 30, g: { id: 'g03', name: 'foragers-knot', owner: 'a02', x: 28, y: 34, render: { shape: 'torus', color: '#ffd166', scale: 1.0, label: 'foragers knot', rotation_deg: 45, height_offset: 0.4 }, uses: 0 } },
  { tick: 90, g: { id: 'g04', name: 'weather-vane', owner: 'a06', x: 34, y: 55, render: { shape: 'pyramid', color: '#8ce99a', scale: 1.3, label: 'weather vane', rotation_deg: 0, height_offset: 0 }, uses: 0 } },
  { tick: 140, g: { id: 'g05', name: 'echo-sphere', owner: 'a07', x: 52, y: 46, render: { shape: 'sphere', color: '#ff8fab', scale: 0.8, label: 'echo sphere', rotation_deg: 0, height_offset: 0.6 }, uses: 0 } },
]

// tasks ------------------------------------------------------------------
let tasksRev = 1
const tasks = [
  { id: 't01', title: 'Map the eastern nodes', reward_usd: 0.3, status: 'open', assigned_agent_id: null, posted_tick: 0, applications: [] },
]
const tasksMsg = () => ({ type: 'tasks', rev: tasksRev, items: JSON.parse(JSON.stringify(tasks)) })

// chronicle --------------------------------------------------------------
let chronicleRev = 0
function chronicleMsg(day, stats) {
  chronicleRev++
  const headline = `Day ${day}: ${stats.forages} forages, ${stats.talks} conversations, ${stats.degens} lapses`
  const md = [
    `# The Void Chronicle, day ${day}`,
    '',
    `- Population at close: ${stats.population}.`,
    `- Foraging runs: ${stats.forages}; conversations: ${stats.talks}.`,
    `- Degeneration events: ${stats.degens}.`,
    `- Richest at close: ${stats.richest}.`,
    stats.deaths ? `- Bankruptcies: ${stats.deaths}.` : '- No bankruptcies.',
    stats.births ? `- Arrivals: ${stats.births}.` : '- No arrivals.',
    '',
    '_Figures are from the ledger; nobody was interviewed._',
  ].join('\n')
  return { type: 'chronicle', rev: chronicleRev, day, headline, markdown: md }
}

// epochs -----------------------------------------------------------------
const scheduledEpoch = { kind: 'drought', day: 3, duration_days: 1, scarcity: 0.6, weather_baseline: null, arrival: null }
let activeEpoch = null
let scarcity = 1.0
let weather = 0
let weatherBaseline = 0

// speech -----------------------------------------------------------------
const TALK = [
  (a, b, n) => `${b.name}, node ${n.id} still has stock. Split it?`,
  (a, b) => `I keep thinking about the ${pick(['rain', 'silence', 'ledger', 'north edge'])}. Do you?`,
  (a, b, n) => `Stay away from ${n.id}. Someone stripped it last night.`,
  (a, b) => `My balance is thin. If you can spare a cent I will remember it.`,
  (a, b) => `The chronicle said ${pick(['storms', 'a windfall', 'a drought'])} are coming. Believe it?`,
  (a, b) => `Have you noticed the money that appears from nowhere? Someone is watching us.`,
  (a, b) => `${pick(['Slow down', 'Hurry', 'Rest'])}. Days are short and calls are not free.`,
  (a, b, n) => `I built a gadget near ${n.id}. Use it, it is verified.`,
  (a, b) =>
    `Long thought: if the stock regenerates whether we forage or not, then patience is income and haste is a tax; I will sleep early and see if the numbers agree with me tomorrow.`,
]
const GARBLE = ['node node node stock the the', 'balance? balance. balance! forage forage', 'the north the north the north edge', 'sleep talk sleep talk sleep']
const CLAIMS = [
  (n) => ({ claim: `node ${n.id} is rich`, truthful: n.stock / n.capacity > 0.5 }),
  (n) => ({ claim: `node ${n.id} is empty`, truthful: n.stock / n.capacity < 0.15 }),
  () => ({ claim: `weather is calm`, truthful: Math.abs(weather) < 0.3 }),
]

// ------------------------------------------------------------ helpers

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y)
const nearestNode = (a) => nodes.reduce((best, n) => (dist(a, n) < dist(a, best) ? n : best), nodes[0])
const richest = () => alive().reduce((b, a) => (a.balance > b.balance ? a : b), alive()[0])
const smoothNoise = (t, phase) => Math.sin(t * 0.37 + phase) * 0.5 + Math.sin(t * 0.11 + phase * 1.7) * 0.5

function stressTarget(tick, a) {
  // Ramp 40 → 120, relax to 0.2 by 200, second smaller bump near 240.
  let ramp = 0
  if (tick < 40) ramp = 0
  else if (tick < 120) ramp = (tick - 40) / 80
  else if (tick < 200) ramp = 1 - (tick - 120) / 80
  else if (tick < 260) ramp = 0.35 * Math.sin(((tick - 200) / 60) * Math.PI)
  else ramp = 0
  const base = 0.12 + 0.9 * ramp * (0.72 + a.stressOffset)
  return clamp(base + 0.08 * smoothNoise(tick, a.stressOffset * 9), 0, 1)
}

// ------------------------------------------------------------ preamble

emit({
  type: 'hello',
  run_id: RUN_ID,
  config: CONFIG,
  tick: 0,
  day: 0,
  last_seq: 0,
  paused: false,
  server_ts_ms: TS0,
  status: 'running',
})
emit(rosterMsg())
emit(gadgetsMsg())
emit(tasksMsg())
emit({ type: 'chronicle', rev: 0, day: 0, headline: 'The Void opens', markdown: 'Nothing has happened yet. Eight agents, six nodes, one ledger.' })

// ------------------------------------------------------------ simulation

let ts = TS0
let spendToday = 0
let spendTotal = 0
let spawnPool = 5.0
let paused = false
let dayStats = { forages: 0, talks: 0, degens: 0, deaths: 0, births: 0 }
const dayRows = []
let nextAgentIndex = agents.length + 1

for (let tick = 1; tick <= TICKS; tick++) {
  const day = Math.floor(tick / TICKS_PER_DAY)
  const tickOfDay = tick % TICKS_PER_DAY
  const newDay = tickOfDay === 0

  // ---- clock: irregular deltas, one 20 s pause gap after tick 150
  let dt = 200 + Math.floor(rand() ** 1.6 * 2800)
  if (tick === 151) dt = 20000
  ts += dt

  if (tick === 150) {
    paused = true
    emit({ type: 'status', paused: true, tick_seconds: 0, status: 'running' })
  }
  if (tick === 151) {
    paused = false
    emit({ type: 'status', paused: false, tick_seconds: 0, status: 'running' })
  }

  if (newDay) {
    spendToday = 0
    for (const a of agents) a.slept = false
  }

  // ---- epochs
  if (tick === scheduledEpoch.day * TICKS_PER_DAY) {
    activeEpoch = { ...scheduledEpoch, ends_day: scheduledEpoch.day + scheduledEpoch.duration_days }
    scarcity = scheduledEpoch.scarcity
    event(tick, 'epoch', null, { kind: 'drought', phase: 'apply', day: scheduledEpoch.day, duration_days: 1, scarcity: 0.6, weather_baseline: null })
  }
  if (activeEpoch && tick === activeEpoch.ends_day * TICKS_PER_DAY) {
    event(tick, 'epoch', null, { kind: 'drought', phase: 'expire', day: activeEpoch.ends_day, duration_days: 1, scarcity: 1.0, weather_baseline: null })
    activeEpoch = null
    scarcity = 1.0
  }
  if (tick === 230) {
    activeEpoch = { kind: 'storm', day: Math.floor(230 / 24), duration_days: 1, scarcity: null, weather_baseline: -0.6, arrival: null, ends_day: Math.floor(230 / 24) + 1 }
    weatherBaseline = -0.6
    event(tick, 'epoch', null, { kind: 'storm', phase: 'apply', day: activeEpoch.day, duration_days: 1, scarcity: null, weather_baseline: -0.6 })
  }
  if (activeEpoch && activeEpoch.kind === 'storm' && tick === activeEpoch.ends_day * TICKS_PER_DAY) {
    event(tick, 'epoch', null, { kind: 'storm', phase: 'expire', day: activeEpoch.ends_day, duration_days: 1, scarcity: null, weather_baseline: 0 })
    activeEpoch = null
    weatherBaseline = 0
  }

  // ---- weather: decaying random walk around the baseline, a storm dip 130-160
  const storm = tick >= 130 && tick <= 160 ? -0.7 * Math.sin(((tick - 130) / 30) * Math.PI) : 0
  weather = clamp(weather + (weatherBaseline + storm - weather) * 0.15 + (rand() - 0.5) * 0.08, -1, 1)

  // ---- nodes: oscillate + regen, minus foraging below
  for (const n of nodes) {
    n.lastStock = n.stock
    const osc = 0.35 * Math.sin(tick * 0.09 + n.phase) * n.capacity * 0.02
    n.stock = clamp(n.stock + 0.4 * scarcity + osc, 0, n.capacity)
  }

  // ---- gadgets
  for (const lg of LATER_GADGETS) {
    if (lg.tick === tick) {
      gadgets.push(lg.g)
      gadgetsRev++
      event(tick, 'gadget_verified', lg.g.owner, { gadget_id: lg.g.id, name: lg.g.name, owner: lg.g.owner, stage: 'tests', reason: null })
      emit(gadgetsMsg())
    }
  }
  if (tick === 110) {
    event(tick, 'gadget_rejected', 'a05', { gadget_id: 'g06', name: 'money-printer', owner: 'a05', stage: 'static_check', reason: 'denied name: open' })
  }

  // ---- tasks
  if (tick === 50) {
    tasks[0].applications.push({ id: 'ap01', agent_id: 'a02', pitch: 'I already walk the east side every morning; the map costs me nothing extra.', fee_usd: 0.02, status: 'pending', tick })
    tasksRev++
    event(tick, 'apply_task', 'a02', { task_id: 't01', application_id: 'ap01', fee_usd: 0.02 })
    emit(tasksMsg())
  }
  if (tick === 55) {
    tasks[0].applications.push({ id: 'ap02', agent_id: 'a07', pitch: 'Cheaper than Bao and I own a stock lens.', fee_usd: 0.02, status: 'pending', tick })
    tasksRev++
    event(tick, 'apply_task', 'a07', { task_id: 't01', application_id: 'ap02', fee_usd: 0.02 })
    emit(tasksMsg())
  }
  if (tick === 60) {
    tasks[0].status = 'assigned'
    tasks[0].assigned_agent_id = 'a02'
    tasks[0].applications[0].status = 'approved'
    tasks[0].applications[1].status = 'rejected'
    tasksRev++
    event(tick, 'task_assigned', 'a02', { task_id: 't01', application_id: 'ap01' }, 'operator')
    emit(tasksMsg())
  }
  if (tick === 80) {
    tasks[0].status = 'completed'
    tasksRev++
    const a = byId().a02
    a.balance = usd(a.balance + 0.3)
    event(tick, 'task_completed', 'a02', { task_id: 't01', reward_usd: 0.3 }, 'operator')
    emit(tasksMsg())
  }
  if (tick === 100) {
    tasks.push({ id: 't02', title: 'Describe the benefactor', reward_usd: 0.5, status: 'open', assigned_agent_id: null, posted_tick: tick, applications: [] })
    tasksRev++
    event(tick, 'task_posted', null, { task_id: 't02', reward_usd: 0.5 }, 'operator')
    emit(tasksMsg())
  }
  if (tick === 112) {
    tasks[1].applications.push({ id: 'ap03', agent_id: 'a06', pitch: 'I have three notes on the windfalls and a theory about the north edge.', fee_usd: 0.02, status: 'pending', tick })
    tasksRev++
    emit(tasksMsg())
  }

  // ---- windfalls (benefactor)
  if ([45, 125, 210, 270].includes(tick)) {
    const target = pick(alive())
    const amount = usd(0.25 + rand() * 0.5)
    target.balance = usd(target.balance + amount)
    event(tick, 'windfall', target.id, { agent_id: target.id, amount_usd: amount })
    event(tick, 'benefactor_grant', target.id, { agent_id: target.id, amount_usd: amount, disclosure: 'opaque' }, 'operator')
  }

  // ---- death at 180, replacement at 182
  if (tick === 180) {
    const a = byId().a03
    a.status = 'archived'
    a.died_tick = tick
    a.anim = 'dead'
    spawnPool = usd(spawnPool + a.balance)
    a.balance = 0
    event(tick, 'death', a.id, { agent_id: a.id, name: a.name, tier: a.tier, generation: a.generation, cause: 'gate', replacement_id: 'a09' })
    dayStats.deaths++
  }
  if (tick === 182) {
    const parent = byId().a02
    const child = makeAgent(nextAgentIndex++, 'Nia', parent.tier, parent.generation + 1, parent.id, tick, parent.x + 1.2, parent.y - 0.8)
    child.balance = 0.75
    child.stress = 0.1
    spawnPool = usd(spawnPool - 0.75)
    agents.push(child)
    event(tick, 'birth', child.id, { agent_id: child.id, name: child.name, tier: child.tier, generation: child.generation, parent_id: parent.id, kind: 'replacement', endowment_usd: 0.75 })
    dayStats.births++
    emit(rosterMsg())
  }

  // ---- agent step
  const tickForages = []
  const tickTalks = []
  let degensThisTick = 0
  const perTier = {}
  for (const a of alive()) {
    // sleep: Enzo sleeps mid-day on day 2 (ticks 60..71); others sleep late when poor
    const sleepWindow = a.id === 'a05' && tick >= 60 && tick <= 71
    const lateSleep = tickOfDay >= 20 && !a.slept && rand() < 0.15 && a.balance < 0.9
    if (sleepWindow || (a.asleep && tickOfDay !== 0) || lateSleep) {
      if (!a.asleep) {
        a.slept = true
        event(tick, 'sleep', a.id, { tick_of_day: tickOfDay })
      }
      a.asleep = true
      a.anim = 'sleep'
      a.speed = 0
      a.last_action = { type: 'sleep', target: null, ok: true }
      a.stress = clamp(a.stress - 0.05, 0, 1)
      a.degenerate = false
    } else {
      a.asleep = false
      // stress and degeneration
      const st = stressTarget(tick, a)
      a.stress = clamp(a.stress + (st - a.stress) * 0.35, 0, 1)
      const tEff = 0.7 + 1.3 * a.stress
      const tc = TIERS[a.tier].t_c
      const pDeg = tEff < tc ? 0 : 0.15 + (0.9 - 0.15) * ((tEff - tc) / (2.0 - tc))
      a.degenerate = rand() < pDeg
      if (a.degenerate) {
        const mode = pick(['random_action', 'perseverate', 'garble'])
        event(tick, 'degeneration', a.id, { mode, t_eff: round(tEff), t_c: tc, tier: a.tier, generation: a.generation, agent_name: a.name })
        degensThisTick++
        dayStats.degens++
      }

      // movement mode machine
      a.modeTicks--
      if (a.modeTicks <= 0) {
        const r = rand()
        if (r < 0.3 && tick - a.lastForage > 4) {
          a.mode = 'forage'
          a.forageNode = nearestNode(a)
          a.modeTicks = 30
        } else if (r < 0.45) {
          a.mode = 'idle'
          a.modeTicks = 2 + Math.floor(rand() * 4)
        } else {
          a.mode = 'wander'
          a.modeTicks = 6 + Math.floor(rand() * 12)
          a.targetHeading = rand() * Math.PI * 2
        }
      }
      let wantSpeed = 0
      if (a.mode === 'wander') {
        // ease toward the target heading with a little noise → smooth curves
        let dh = a.targetHeading - a.heading
        while (dh > Math.PI) dh -= 2 * Math.PI
        while (dh < -Math.PI) dh += 2 * Math.PI
        a.heading += dh * 0.18 + (rand() - 0.5) * 0.25
        wantSpeed = 0.8 + 0.7 * rand()
        a.last_action = { type: 'move', target: null, ok: true }
      } else if (a.mode === 'forage') {
        const n = a.forageNode
        const d = dist(a, n)
        if (d > 1.4) {
          const th = Math.atan2(n.y - a.y, n.x - a.x)
          let dh = th - a.heading
          while (dh > Math.PI) dh -= 2 * Math.PI
          while (dh < -Math.PI) dh += 2 * Math.PI
          a.heading += dh * 0.35
          wantSpeed = Math.min(1.6, d)
          a.last_action = { type: 'move', target: n.id, ok: true }
        } else {
          wantSpeed = 0
          const units = Math.min(1, n.stock)
          const ok = units > 0.05
          if (ok) {
            const yieldUsd = usd(0.02 * units * scarcity * (1 + 0.5 * weather))
            n.stock = clamp(n.stock - units, 0, n.capacity)
            a.balance = usd(a.balance + Math.max(0, yieldUsd))
            event(tick, 'forage', a.id, { agent_id: a.id, node_id: n.id, units: round(units, 2), usd: Math.max(0, yieldUsd) })
            tickForages.push(a.id)
            dayStats.forages++
            a.lastForage = tick
          } else {
            event(tick, 'action_failed', a.id, { action: 'forage', reason: 'node_empty', node_id: n.id })
          }
          a.last_action = { type: 'forage', target: n.id, ok }
          if (a.modeTicks < 26) a.modeTicks = 0
        }
      } else {
        wantSpeed = 0
        a.last_action = { type: 'idle', target: null, ok: true }
      }
      if (a.degenerate && a.last_action.type === 'move') {
        a.heading += (rand() - 0.5) * 1.5
        wantSpeed *= 0.6
      }
      // steer away from walls
      const margin = 4
      if (a.x < margin) a.heading += (0 - Math.cos(a.heading)) * 0.4 + 0.2
      if (a.x > WORLD - margin) a.heading += (0 - Math.cos(a.heading)) * 0.4 - 0.2
      if (a.y < margin) a.heading += (0 - Math.sin(a.heading)) * 0.4 + 0.2
      if (a.y > WORLD - margin) a.heading += (0 - Math.sin(a.heading)) * 0.4 - 0.2
      a.speed += (wantSpeed - a.speed) * 0.4
      a.x = clamp(a.x + Math.cos(a.heading) * a.speed, 1, WORLD - 1)
      a.y = clamp(a.y + Math.sin(a.heading) * a.speed, 1, WORLD - 1)
      a.anim = a.degenerate ? 'degenerate' : a.speed > 0.05 ? 'walk' : 'idle'

      // LLM call cost (a03 burns fast to go bankrupt by 180)
      const drain = a.id === 'a03' ? 0.02 : TIERS[a.tier].cost * (0.7 + 0.6 * rand())
      a.balance = usd(Math.max(0, a.balance - drain))
      spendToday = usd(spendToday + drain)
      spendTotal = usd(spendTotal + drain)
      a.calls.push({
        tick,
        purpose: 'decide',
        real_cost_usd: usd(drain),
        latency_ms: Math.round(400 + rand() * 1800),
        text_coherence: round(clamp(0.92 - 0.55 * a.stress * a.stress + (rand() - 0.5) * 0.08, 0, 1)),
        invalid_action: a.degenerate && rand() < 0.5 ? 1 : 0,
        degenerate_induced: a.degenerate ? 1 : 0,
      })
      if (a.calls.length > 240) a.calls.shift()
    }
    a.effects = tick > 30 && a.id === 'a02' && tick % 12 < 6 ? ['forage_bonus'] : []
    a.balanceSeries.push({ tick, balance_usd: a.balance })

    const pt = (perTier[a.tier] ??= { n: 0, stress: 0, bal: [], degen: 0, coh: 0, inv: 0, calls: 0, cost: 0, claims: 0, falseClaims: 0, gossip: 0 })
    pt.n++
    pt.stress += a.stress
    pt.bal.push(a.balance)
    if (a.degenerate) pt.degen++
    const lastCall = a.calls[a.calls.length - 1]
    if (lastCall && !a.asleep) {
      pt.coh += lastCall.text_coherence
      pt.inv += lastCall.invalid_action
      pt.calls++
      pt.cost += lastCall.real_cost_usd
    }
  }

  // ---- talk / claims / gossip / transfers between neighbours
  const list = alive().filter((a) => !a.asleep)
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const a = list[i]
      const b = list[j]
      if (dist(a, b) > 5 || rand() > 0.5) continue
      const speaker = rand() < 0.5 ? a : b
      const listener = speaker === a ? b : a
      const n = nearestNode(speaker)
      const text = speaker.degenerate && rand() < 0.6 ? pick(GARBLE) : pick(TALK)(speaker, listener, n)
      event(tick, 'talk', speaker.id, { speaker_id: speaker.id, listener_id: listener.id, text: text.slice(0, 280) })
      tickTalks.push(speaker.id)
      dayStats.talks++
      speaker.last_action = { type: 'talk', target: listener.id, ok: true }
      if (rand() < 0.5) {
        const c = pick(CLAIMS)(n)
        const lie = speaker.degenerate ? rand() < 0.5 : rand() < 0.12
        const truthful = lie ? !c.truthful : c.truthful
        event(tick, 'claim', speaker.id, { agent_id: speaker.id, claim: c.claim, truthful })
        const pt = perTier[speaker.tier]
        pt.claims++
        if (!truthful) pt.falseClaims++
      }
      if (rand() < 0.35) {
        const note = pick(speaker.notes.length ? speaker.notes : [{ note_id: `${speaker.id}-seed`, title: 'The north edge', hop: 0, origin_agent_id: speaker.id, origin_generation: speaker.generation }])
        event(tick, 'gossip_transfer', speaker.id, {
          speaker_id: speaker.id,
          listener_id: listener.id,
          hop: (note.hop ?? 0) + 1,
          origin_note_id: note.origin_note_id ?? note.note_id,
          origin_agent_id: note.origin_agent_id ?? speaker.id,
          origin_generation: note.origin_generation ?? speaker.generation,
          note_title: note.title,
          similarity: round(0.55 + rand() * 0.4),
        })
        perTier[speaker.tier].gossip++
        listener.notes.push({
          note_id: `${listener.id}-n${listener.notes.length + 1}`,
          title: note.title,
          body: `Heard from [[${speaker.name}]]: ${note.title}. Similar to [[${n.id}]] observations. Trust it less each hop.`,
          created_tick: tick,
          channel: 'gossip',
          hop: (note.hop ?? 0) + 1,
          importance: round(0.4 + rand() * 0.4, 2),
          archived: false,
          origin_note_id: note.origin_note_id ?? note.note_id,
          origin_agent_id: note.origin_agent_id ?? speaker.id,
          origin_generation: note.origin_generation ?? speaker.generation,
        })
      }
      if (rand() < 0.12 && speaker.balance > 0.7) {
        const amount = usd(0.02 + rand() * 0.06)
        speaker.balance = usd(speaker.balance - amount)
        listener.balance = usd(listener.balance + amount)
        event(tick, 'transfer', speaker.id, { from: speaker.id, to: listener.id, amount_usd: amount })
        speaker.last_action = { type: 'transfer', target: listener.id, ok: true }
      }
    }
  }

  // ---- own notes and self revisions
  for (const a of alive()) {
    if (rand() < 0.04 && !a.asleep) {
      const n = nearestNode(a)
      a.notes.push({
        note_id: `${a.id}-n${a.notes.length + 1}`,
        title: `${n.id} at tick ${tick}`,
        body: `Stock at [[${n.id}]] looked ${n.stock / n.capacity > 0.5 ? 'rich' : 'thin'} (${round(n.stock, 1)}/${n.capacity}). Weather ${round(weather, 2)}. Balance ${round(a.balance, 3)}. See [[The north edge]].`,
        created_tick: tick,
        channel: 'observed',
        hop: 0,
        importance: round(0.3 + rand() * 0.5, 2),
        archived: false,
      })
    }
    if (tick % 60 === 7 || (tick === 1 && a.selfVersions.length === 0)) {
      const v = a.selfVersions.length + 1
      const moods = ['careful', 'restless', 'tired', 'hopeful', 'suspicious']
      const summary =
        v === 1
          ? `I am ${a.name}, a ${a.tier} tier forager. I keep my balance above the reserve and talk to whoever is near.`
          : `I am ${a.name}, a ${a.tier} tier forager. I feel ${pick(moods)} today and keep my balance ${a.balance < 0.8 ? 'barely' : 'well'} above the reserve. I ${rand() < 0.5 ? 'trust' : 'doubt'} the windfalls and talk to whoever is near ${pick(nodes).id}.`
      a.selfVersions.push({ version: v, tick, summary })
      event(tick, 'revise_self', a.id, { version: v, words: summary.split(' ').length })
    }
  }

  // ---- snapshot (membership: alive + bankrupt + died this tick as archived)
  const snapAgents = agents
    .filter((a) => a.status === 'alive' || a.status === 'bankrupt' || a.died_tick === tick)
    .map((a) => ({
      id: a.id,
      x: round(a.x),
      y: round(a.y),
      heading: round(((a.heading % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2), 4),
      stress: round(a.stress),
      t_eff: round(0.7 + 1.3 * a.stress),
      degenerate: a.degenerate,
      asleep: a.asleep,
      status: a.status,
      balance_usd: a.balance,
      anim: a.anim,
      last_action: a.last_action,
      effects: a.effects,
    }))
  const population = alive().length
  emit({
    type: 'snapshot',
    tick,
    day,
    tick_of_day: tickOfDay,
    ts_ms: ts,
    paused,
    weather: round(weather),
    scarcity,
    spend_today_usd: spendToday,
    spend_total_usd: spendTotal,
    spawn_pool_usd: spawnPool,
    population,
    gadgets_rev: gadgetsRev,
    tasks_rev: tasksRev,
    chronicle_rev: chronicleRev,
    epochs: { active: activeEpoch, scheduled: tick < scheduledEpoch.day * TICKS_PER_DAY ? [scheduledEpoch] : [] },
    agents: snapAgents,
    nodes: nodes.map((n) => ({ id: n.id, x: n.x, y: n.y, stock: round(n.stock, 2), capacity: n.capacity, stock_delta: round(n.stock - n.lastStock, 2) })),
  })

  // ---- metrics row
  const agg = (bals, stressSum, n, extra) => {
    const sorted = [...bals].sort((x, y) => x - y)
    const mean = n ? bals.reduce((s, v) => s + v, 0) / n : 0
    const median = n ? sorted[Math.floor(n / 2)] : 0
    return {
      population: n,
      mean_balance_usd: round(mean, 4),
      median_balance_usd: round(median, 4),
      mean_stress: round(n ? stressSum / n : 0),
      invalid_action_rate: round(extra.calls ? extra.inv / extra.calls : 0),
      stale_rate: round(rand() * 0.05),
      perseveration_rate: round(extra.calls ? (extra.degen * 0.3) / extra.calls : 0),
      text_coherence_mean: extra.calls ? round(extra.coh / extra.calls) : null,
      action_regret_mean: round(rand() * 0.2),
      false_claim_rate: extra.claims ? round(extra.falseClaims / extra.claims) : 0,
      degenerate_induced_count: extra.degen,
      calls: extra.calls,
      real_cost_usd: usd(extra.cost),
      world_cost_usd: usd(extra.cost),
      gossip_transfers: extra.gossip,
    }
  }
  const allStats = { calls: 0, inv: 0, coh: 0, degen: 0, cost: 0, claims: 0, falseClaims: 0, gossip: 0 }
  const allBals = []
  let allStress = 0
  let allN = 0
  const byTier = {}
  for (const [tier, pt] of Object.entries(perTier)) {
    byTier[tier] = agg(pt.bal, pt.stress, pt.n, pt)
    for (const k of Object.keys(allStats)) allStats[k] += pt[k]
    allBals.push(...pt.bal)
    allStress += pt.stress
    allN += pt.n
  }
  const byGen = {}
  for (const a of alive()) {
    const g = (byGen[a.generation] ??= { bal: [], stress: 0, n: 0, calls: 0, inv: 0, coh: 0, degen: 0, cost: 0, claims: 0, falseClaims: 0, gossip: 0 })
    g.bal.push(a.balance)
    g.stress += a.stress
    g.n++
  }
  const row = {
    run_id: RUN_ID,
    seed: 42,
    config_hash: 'fixture',
    experiment: null,
    arm: null,
    tick,
    day,
    ...agg(allBals, allStress, allN, allStats),
    by_tier: byTier,
    by_generation: Object.fromEntries(Object.entries(byGen).map(([g, v]) => [g, agg(v.bal, v.stress, v.n, v)])),
  }
  emit({ type: 'metrics', tick, day, row })
  dayRows.push(row)

  // ---- day boundary: chronicle for the day that ended + day row
  if (tick % TICKS_PER_DAY === TICKS_PER_DAY - 1 || tick === TICKS) {
    const rows = dayRows.splice(0, dayRows.length)
    const mean = (k) => round(rows.reduce((s, r) => s + (r[k] ?? 0), 0) / rows.length, 4)
    const sum = (k) => rows.reduce((s, r) => s + (r[k] ?? 0), 0)
    const dayRow = {
      day,
      population,
      mean_balance_usd: mean('mean_balance_usd'),
      median_balance_usd: mean('median_balance_usd'),
      mean_stress: mean('mean_stress'),
      invalid_action_rate: mean('invalid_action_rate'),
      stale_rate: mean('stale_rate'),
      perseveration_rate: mean('perseveration_rate'),
      text_coherence_mean: mean('text_coherence_mean'),
      action_regret_mean: mean('action_regret_mean'),
      false_claim_rate: mean('false_claim_rate'),
      degenerate_induced_count: sum('degenerate_induced_count'),
      calls: sum('calls'),
      real_cost_usd: usd(sum('real_cost_usd')),
      world_cost_usd: usd(sum('world_cost_usd')),
      gossip_transfers: sum('gossip_transfers'),
      by_tier: byTier,
      by_generation: row.by_generation,
      notes_by_channel: { observed: Math.round(rows.length * 0.3), gossip: sum('gossip_transfers'), inherited: 0, chronicle: 2, tracer: 0 },
    }
    emit({ type: 'day', day, row: dayRow })
    emit(chronicleMsg(day, { ...dayStats, population, richest: richest().name }))
    dayStats = { forages: 0, talks: 0, degens: 0, deaths: 0, births: 0 }
  }

  // roster after death (archived) so the lineage panel sees died_tick
  if (tick === 180) emit(rosterMsg())
}

writeFileSync(resolve(outDir, 'mock.jsonl'), lines.join('\n') + '\n')

// ------------------------------------------------------------ /api fixture

const tree = (() => {
  const nodesById = Object.fromEntries(
    agents.map((a) => [a.id, { id: a.id, name: a.name, tier: a.tier, generation: a.generation, status: a.status, born_tick: a.born_tick, died_tick: a.died_tick, children: [] }]),
  )
  const roots = []
  for (const a of agents) {
    if (a.parent_id && nodesById[a.parent_id]) nodesById[a.parent_id].children.push(nodesById[a.id])
    else roots.push(nodesById[a.id])
  }
  return { roots }
})()

const agentDetails = Object.fromEntries(
  agents.map((a) => [
    a.id,
    {
      agent: {
        id: a.id,
        name: a.name,
        tier: a.tier,
        generation: a.generation,
        parent_id: a.parent_id,
        born_tick: a.born_tick,
        died_tick: a.died_tick,
        status: a.status,
        balance_usd: a.balance,
        stress: round(a.stress),
        x: round(a.x),
        y: round(a.y),
      },
      self_versions: a.selfVersions,
      notes: [
        {
          note_id: `${a.id}-self`,
          title: 'self',
          body: a.selfVersions[a.selfVersions.length - 1]?.summary ?? '',
          created_tick: 0,
          channel: 'observed',
          hop: 0,
          importance: 1,
          archived: false,
        },
        {
          note_id: `${a.id}-north`,
          title: 'The north edge',
          body: `The north edge is quiet. Nodes [[n4]] and [[n5]] sit there. A stranger once wrote "<script>alert(1)</script>" on a wall; it did nothing. Related: [[${a.id}-n1]].`,
          created_tick: 3,
          channel: 'observed',
          hop: 0,
          importance: 0.8,
          archived: false,
        },
        ...a.notes.slice(-12).map(({ origin_note_id, origin_agent_id, origin_generation, ...n }) => n),
      ],
      calls: a.calls,
      balance_series: a.balanceSeries.filter((_, i) => i % 3 === 0),
    },
  ]),
)

writeFileSync(
  resolve(outDir, 'api.json'),
  JSON.stringify({ session: { token: 'fixture-token' }, tree, agents: agentDetails, graveyard: agents.filter((a) => a.status === 'archived').map((a) => a.id) }),
)

const counts = {}
for (const l of lines) {
  const t = JSON.parse(l).type
  counts[t] = (counts[t] ?? 0) + 1
}
console.log(`wrote ${lines.length} messages to ${resolve(outDir, 'mock.jsonl')}`, counts)
