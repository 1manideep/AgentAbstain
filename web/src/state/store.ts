/**
 * Low-churn React state (DESIGN §16). Per-frame data never lives here: the scene
 * reads `history` directly inside its frame loop. Chart-facing aggregates are
 * mutated in place by O(1) reducers and published to React through `rev`
 * counters at most 4× per second (trailing throttle).
 */
import { create } from 'zustand'
import type {
  ChronicleMsg,
  DayRow,
  EventMsg,
  GadgetsMsg,
  HelloConfig,
  HelloMsg,
  LastAction,
  MetricsRow,
  RosterAgent,
  RunStatus,
  SnapshotMsg,
  StatusMsg,
  TasksMsg,
} from '../protocol'
import { throttle } from '../util/throttle'
import { EventRing, HEARTBEAT_KINDS, type EventFilter } from './events'
import type { PlaybackMode, PlaybackSpeed } from './history'
import { Series } from './series'

export type ConnState = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'fixture'
export type FeedMode = 'ws' | 'fixture'

export interface AgentStat {
  id: string
  x: number
  y: number
  balance: number
  stress: number
  tEff: number
  asleep: boolean
  degenerate: boolean
  status: RosterAgent['status']
  anim: string
  lastAction: LastAction | null
  effects: string[]
  tick: number
}

export interface WorldStats {
  tick: number
  day: number
  tickOfDay: number
  tsMs: number
  weather: number
  scarcity: number
  spendToday: number
  spendTotal: number
  spawnPool: number
  population: number
  paused: boolean
  epochs: SnapshotMsg['epochs']
}

export interface ClockView {
  tick: number
  day: number
  tickOfDay: number
  frameIndex: number
  frameCount: number
  newestTick: number
  mode: PlaybackMode
  speed: PlaybackSpeed
  rate: number
  lagMs: number
}

export interface PerfView {
  fps: number
  calls: number
  dpr: number
}

const EMPTY_WORLD: WorldStats = {
  tick: 0,
  day: 0,
  tickOfDay: 0,
  tsMs: 0,
  weather: 0,
  scarcity: 1,
  spendToday: 0,
  spendTotal: 0,
  spawnPool: 0,
  population: 0,
  paused: false,
  epochs: { active: null, scheduled: [] },
}

export const COMMIT_HZ = 4

export interface VoidState {
  conn: ConnState
  feedMode: FeedMode
  runId: string | null
  config: HelloConfig | null
  runStatus: RunStatus | null
  serverPaused: boolean
  tickSeconds: number
  lastSeq: number

  roster: RosterAgent[]
  rosterById: Map<string, RosterAgent>
  rosterRev: number

  world: WorldStats
  /** Mutable map; read under `statsRev`. */
  agentStats: Map<string, AgentStat>
  statsRev: number

  clock: ClockView
  perf: PerfView

  gadgets: GadgetsMsg | null
  tasks: TasksMsg | null
  chronicle: ChronicleMsg | null

  /** Stable ring; read under `eventsRev`. */
  events: EventRing
  eventsRev: number
  filter: { kinds: string[] | null; exclude: string[] | null; agentId: string | null }

  /** Stable aggregates; read under `seriesRev`. */
  series: Series
  seriesRev: number

  selectedAgentId: string | null
  hoveredAgentId: string | null
  hoveredGadgetId: string | null
  followAgentId: string | null
  playback: { mode: PlaybackMode; speed: PlaybackSpeed }
  reducedMotion: boolean
  /** Subtle world-grid overlay on the terrain (HUD toggle, persisted). */
  showGrid: boolean

  // reducers (called by the feed)
  setConn: (c: ConnState, mode?: FeedMode) => void
  applyHello: (m: HelloMsg) => void
  applyRoster: (agents: RosterAgent[]) => void
  applySnapshot: (m: SnapshotMsg) => void
  applyEvent: (e: EventMsg) => boolean
  applyMetrics: (row: MetricsRow) => void
  applyDay: (row: DayRow) => void
  applyGadgets: (m: GadgetsMsg) => void
  applyTasks: (m: TasksMsg) => void
  applyChronicle: (m: ChronicleMsg) => void
  applyStatus: (m: StatusMsg) => void
  resetRun: () => void

  // ui
  setFilter: (f: { kinds: string[] | null; exclude: string[] | null; agentId: string | null }) => void
  select: (id: string | null) => void
  hover: (id: string | null) => void
  hoverGadget: (id: string | null) => void
  follow: (id: string | null) => void
  setPlayback: (p: { mode: PlaybackMode; speed: PlaybackSpeed }) => void
  commitClock: (c: ClockView) => void
  setPerf: (p: Partial<PerfView>) => void
  toggleGrid: () => void
}

const worldAcc: WorldStats = { ...EMPTY_WORLD }
const statsAcc = new Map<string, AgentStat>()
const seriesAcc = new Series()
const ringAcc = new EventRing(2000)

let pendingStats = false
let pendingSeries = false
let pendingEvents = false

export const useStore = create<VoidState>()((set, get) => {
  const commit = throttle(() => {
    const patch: Partial<VoidState> = {}
    const s = get()
    if (pendingStats) {
      patch.statsRev = s.statsRev + 1
      patch.world = { ...worldAcc }
      pendingStats = false
    }
    if (pendingSeries) {
      patch.seriesRev = s.seriesRev + 1
      pendingSeries = false
    }
    if (pendingEvents) {
      patch.eventsRev = ringAcc.rev
      pendingEvents = false
    }
    if (Object.keys(patch).length) set(patch)
  }, 1000 / COMMIT_HZ)

  return {
    conn: 'connecting',
    feedMode: 'ws',
    runId: null,
    config: null,
    runStatus: null,
    serverPaused: false,
    tickSeconds: 0,
    lastSeq: 0,
    roster: [],
    rosterById: new Map(),
    rosterRev: 0,
    world: EMPTY_WORLD,
    agentStats: statsAcc,
    statsRev: 0,
    clock: {
      tick: 0,
      day: 0,
      tickOfDay: 0,
      frameIndex: -1,
      frameCount: 0,
      newestTick: 0,
      mode: 'live',
      speed: 1,
      rate: 1,
      lagMs: 0,
    },
    perf: { fps: 0, calls: 0, dpr: 1 },
    gadgets: null,
    tasks: null,
    chronicle: null,
    events: ringAcc,
    eventsRev: 0,
    filter: { kinds: null, exclude: [...HEARTBEAT_KINDS], agentId: null },
    series: seriesAcc,
    seriesRev: 0,
    selectedAgentId: null,
    hoveredAgentId: null,
    hoveredGadgetId: null,
    followAgentId: null,
    playback: { mode: 'live', speed: 1 },
    reducedMotion:
      typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
        : false,
    showGrid: readGridPref(),

    setConn: (conn, mode) => set(mode ? { conn, feedMode: mode } : { conn }),

    applyHello: (m) => {
      const s = get()
      if (s.runId && s.runId !== m.run_id) s.resetRun()
      set({
        runId: m.run_id,
        config: m.config,
        runStatus: m.status,
        serverPaused: m.paused,
        tickSeconds: m.config.tick_seconds,
      })
    },

    applyRoster: (agents) => {
      const byId = new Map<string, RosterAgent>()
      for (const a of agents) byId.set(a.id, a)
      set({ roster: agents, rosterById: byId, rosterRev: get().rosterRev + 1 })
    },

    applySnapshot: (m) => {
      const s = get()
      worldAcc.tick = m.tick
      worldAcc.day = m.day
      worldAcc.tickOfDay = m.tick_of_day
      worldAcc.tsMs = m.ts_ms
      worldAcc.weather = m.weather
      worldAcc.scarcity = m.scarcity
      worldAcc.spendToday = m.spend_today_usd
      worldAcc.spendTotal = m.spend_total_usd
      worldAcc.spawnPool = m.spawn_pool_usd
      worldAcc.population = m.population
      worldAcc.paused = m.paused
      worldAcc.epochs = m.epochs
      for (const a of m.agents) {
        let st = statsAcc.get(a.id)
        if (!st) {
          st = {
            id: a.id,
            x: a.x,
            y: a.y,
            balance: a.balance_usd,
            stress: a.stress,
            tEff: typeof a.t_eff === 'number' ? a.t_eff : Number.NaN,
            asleep: a.asleep,
            degenerate: a.degenerate,
            status: a.status,
            anim: a.anim,
            lastAction: a.last_action,
            effects: a.effects,
            tick: m.tick,
          }
          statsAcc.set(a.id, st)
        } else {
          st.x = a.x
          st.y = a.y
          st.balance = a.balance_usd
          st.stress = a.stress
          st.tEff = typeof a.t_eff === 'number' ? a.t_eff : Number.NaN
          st.asleep = a.asleep
          st.degenerate = a.degenerate
          st.status = a.status
          st.anim = a.anim
          st.lastAction = a.last_action
          st.effects = a.effects
          st.tick = m.tick
        }
      }
      seriesAcc.onSnapshot(m, s.rosterById)
      pendingStats = true
      pendingSeries = true
      if (m.paused !== s.serverPaused) set({ serverPaused: m.paused })
      commit.call()
    },

    applyEvent: (e) => {
      const s = get()
      if (!ringAcc.push(e)) return false
      seriesAcc.onEvent(e, s.rosterById)
      pendingEvents = true
      pendingSeries = true
      if (e.seq > s.lastSeq) set({ lastSeq: e.seq })
      commit.call()
      return true
    },

    applyMetrics: (row) => {
      seriesAcc.onMetrics(row)
      pendingSeries = true
      commit.call()
    },

    applyDay: (row) => {
      seriesAcc.onDay(row)
      pendingSeries = true
      commit.call()
    },

    applyGadgets: (m) => {
      const cur = get().gadgets
      if (cur && cur.rev >= m.rev) return
      set({ gadgets: m })
    },

    applyTasks: (m) => {
      const cur = get().tasks
      if (cur && cur.rev >= m.rev) return
      set({ tasks: m })
    },

    applyChronicle: (m) => {
      const cur = get().chronicle
      if (cur && cur.rev >= m.rev) return
      set({ chronicle: m })
    },

    applyStatus: (m) => set({ serverPaused: m.paused, tickSeconds: m.tick_seconds, runStatus: m.status }),

    resetRun: () => {
      statsAcc.clear()
      seriesAcc.clear()
      ringAcc.clear()
      Object.assign(worldAcc, EMPTY_WORLD)
      pendingStats = pendingSeries = pendingEvents = false
      set({
        roster: [],
        rosterById: new Map(),
        rosterRev: get().rosterRev + 1,
        world: { ...EMPTY_WORLD },
        statsRev: get().statsRev + 1,
        seriesRev: get().seriesRev + 1,
        eventsRev: ringAcc.rev,
        lastSeq: 0,
        gadgets: null,
        tasks: null,
        chronicle: null,
        selectedAgentId: null,
        hoveredAgentId: null,
        followAgentId: null,
      })
    },

    setFilter: (f) => {
      const ef: EventFilter = { kinds: f.kinds ? new Set(f.kinds) : null, exclude: f.exclude ? new Set(f.exclude) : null, agentId: f.agentId }
      ringAcc.setFilter(ef)
      set({ filter: f, eventsRev: ringAcc.rev })
    },
    select: (id) => set({ selectedAgentId: id }),
    hover: (id) => {
      if (get().hoveredAgentId !== id) set({ hoveredAgentId: id })
    },
    hoverGadget: (id) => {
      if (get().hoveredGadgetId !== id) set({ hoveredGadgetId: id })
    },
    follow: (id) => set({ followAgentId: id }),
    setPlayback: (p) => {
      const cur = get().playback
      if (cur.mode !== p.mode || cur.speed !== p.speed) set({ playback: p })
    },
    commitClock: (c) => set({ clock: c }),
    setPerf: (p) => set({ perf: { ...get().perf, ...p } }),
    toggleGrid: () => {
      const v = !get().showGrid
      try {
        localStorage.setItem('void.grid', v ? '1' : '0')
      } catch {
        /* ignore */
      }
      set({ showGrid: v })
    },
  }
})

function readGridPref(): boolean {
  try {
    return localStorage.getItem('void.grid') === '1'
  } catch {
    return false
  }
}

export const selectTierColor = (config: HelloConfig | null, tier: string): string => {
  const c = config?.tiers[tier]?.color
  return c && /^#[0-9a-f]{6}$/i.test(c) ? c : FALLBACK_TIER_COLORS[hashTier(tier) % FALLBACK_TIER_COLORS.length]!
}

/** dataviz categorical slots (dark), used when a tier colour is missing or malformed. */
export const FALLBACK_TIER_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9', '#e66767']

function hashTier(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return h
}
