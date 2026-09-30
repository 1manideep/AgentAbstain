/**
 * Wire types for every server → client message in DESIGN §15 and the /api
 * response shapes the client consumes. Field names here are the contract:
 * nothing is renamed on the way in. Where §15 leaves a nested shape open
 * (metrics rows, epochs, /api/tree, /api/agents/{id}) the shape is fixed here
 * and called out in the report so the server can match it.
 */

export type RunStatus = 'running' | 'completed' | 'capped_total' | 'extinct' | 'inconsistent'
export type AgentStatus = 'alive' | 'bankrupt' | 'archived'
export type DegenerationMode = 'induce' | 'observe' | 'both'
export type Visibility = 'public' | 'operator'
export type EpochKind = 'drought' | 'storm' | 'boom' | 'arrival' | 'custom'
export type GadgetShape = 'cube' | 'sphere' | 'pyramid' | 'cylinder' | 'torus'
export const GADGET_SHAPES: readonly GadgetShape[] = ['cube', 'sphere', 'pyramid', 'cylinder', 'torus']

/** Procedural animation states the scene knows how to blend. Unknown values map to idle. */
export type AnimState = 'idle' | 'walk' | 'sleep' | 'degenerate' | 'dead'

// ---------------------------------------------------------------- hello

export interface TierConfig {
  color: string
  model: string
  provider: string
}

export interface HelloConfig {
  world_size: number
  ticks_per_day: number
  tick_seconds: number
  render_delay_ticks: number
  population_cap: number
  daily_cap_usd: number
  total_cap_usd: number
  tiers: Record<string, TierConfig>
  /** Not in §15: needed to label the T_eff histogram honestly (§8, §16). */
  degeneration_mode?: DegenerationMode
}

export interface HelloMsg {
  type: 'hello'
  run_id: string
  config: HelloConfig
  tick: number
  day: number
  last_seq: number
  paused: boolean
  server_ts_ms: number
  status: RunStatus
}

// ---------------------------------------------------------------- roster

export interface RosterAgent {
  id: string
  name: string
  tier: string
  generation: number
  parent_id: string | null
  born_tick: number
  died_tick: number | null
  status: AgentStatus
}

export interface RosterMsg {
  type: 'roster'
  agents: RosterAgent[]
}

// ---------------------------------------------------------------- snapshot

export interface LastAction {
  type: string
  target: string | null
  ok: boolean
}

export interface SnapshotAgent {
  id: string
  x: number
  y: number
  heading: number
  stress: number
  t_eff: number
  degenerate: boolean
  asleep: boolean
  status: AgentStatus
  balance_usd: number
  anim: AnimState | string
  last_action: LastAction | null
  effects: string[]
}

export interface SnapshotNode {
  id: string
  x: number
  y: number
  stock: number
  capacity: number
  stock_delta: number
}

/** §11.4: `{...}` in §15. Fixed here to the config epoch shape plus its bounds. */
export interface Epoch {
  kind: EpochKind
  day: number
  duration_days: number
  scarcity?: number | null
  weather_baseline?: number | null
  arrival?: { name: string; tier: string; balance_usd: number } | null
  /** Set by the server when the epoch is active. */
  ends_day?: number
}

export interface SnapshotMsg {
  type: 'snapshot'
  tick: number
  day: number
  tick_of_day: number
  ts_ms: number
  paused: boolean
  weather: number
  scarcity: number
  spend_today_usd: number
  spend_total_usd: number
  spawn_pool_usd: number
  population: number
  gadgets_rev: number
  tasks_rev: number
  chronicle_rev: number
  epochs: { active: Epoch | null; scheduled: Epoch[] }
  agents: SnapshotAgent[]
  nodes: SnapshotNode[]
}

// ---------------------------------------------------------------- gadgets

export interface GadgetRender {
  shape: GadgetShape
  color: string
  scale: number
  label: string
  rotation_deg?: number
  height_offset?: number
}

export interface GadgetItem {
  id: string
  name: string
  owner: string
  x: number
  y: number
  render: GadgetRender
  uses: number
}

export interface GadgetsMsg {
  type: 'gadgets'
  rev: number
  items: GadgetItem[]
}

// ---------------------------------------------------------------- tasks

export type TaskStatus = 'open' | 'assigned' | 'completed' | 'cancelled'
export type ApplicationStatus = 'pending' | 'approved' | 'rejected'

export interface TaskApplication {
  id: string
  agent_id: string
  pitch: string
  fee_usd: number
  status: ApplicationStatus
  tick: number
}

export interface TaskItem {
  id: string
  title: string
  reward_usd: number
  status: TaskStatus
  assigned_agent_id: string | null
  posted_tick: number
  applications: TaskApplication[]
}

export interface TasksMsg {
  type: 'tasks'
  rev: number
  items: TaskItem[]
}

// ---------------------------------------------------------------- chronicle

export interface ChronicleMsg {
  type: 'chronicle'
  rev: number
  day: number
  headline: string
  /** Rendered as plain text only (§16, §18): never converted to HTML. */
  markdown: string
}

// ---------------------------------------------------------------- events

export interface DegenerationPayload {
  mode: 'random_action' | 'perseverate' | 'garble' | string
  t_eff: number
  t_c: number
  tier: string
  generation: number
  agent_name: string
}

export interface GossipTransferPayload {
  speaker_id: string
  listener_id: string
  hop: number
  origin_note_id: string
  origin_agent_id: string
  origin_generation: number
  note_title: string
  similarity: number
}

export interface DeathPayload {
  agent_id: string
  name: string
  tier: string
  generation: number
  cause: 'gate' | 'overrun' | string
  replacement_id: string | null
}

export interface BirthPayload {
  agent_id: string
  name: string
  tier: string
  generation: number
  parent_id: string | null
  kind: 'replacement' | 'offspring' | 'arrival' | 'initial' | string
  endowment_usd: number
}

export interface TalkPayload {
  speaker_id: string
  listener_id: string
  text: string
}

export interface TransferPayload {
  from: string
  to: string
  amount_usd: number
}

export interface ForagePayload {
  agent_id: string
  node_id: string
  units: number
  usd: number
}

export interface WindfallPayload {
  agent_id: string
  amount_usd: number
}

export interface ClaimPayload {
  agent_id: string
  claim: string
  truthful: boolean
}

export interface EpochPayload {
  kind: EpochKind
  day?: number
  duration_days?: number
  scarcity?: number | null
  weather_baseline?: number | null
  phase?: 'apply' | 'expire' | string
  [extra: string]: unknown
}

export interface GadgetGatePayload {
  gadget_id: string
  name: string
  owner: string
  stage: string
  reason: string | null
}

export interface EventPayloadByKind {
  degeneration: DegenerationPayload
  gossip_transfer: GossipTransferPayload
  death: DeathPayload
  birth: BirthPayload
  talk: TalkPayload
  transfer: TransferPayload
  forage: ForagePayload
  windfall: WindfallPayload
  claim: ClaimPayload
  epoch: EpochPayload
  gadget_verified: GadgetGatePayload
  gadget_rejected: GadgetGatePayload
}

export type KnownEventKind = keyof EventPayloadByKind

interface EventBase {
  type: 'event'
  seq: number
  tick: number
  day: number
  agent_id: string | null
  visibility: Visibility
}

export type TypedEventMsg = {
  [K in KnownEventKind]: EventBase & { kind: K; payload: EventPayloadByKind[K] }
}[KnownEventKind]

/** Any other kind from void.events.Kind (move, sleep, task_posted, ...) with a free payload. */
export interface OtherEventMsg extends EventBase {
  kind: string
  payload: Record<string, unknown>
}

export type EventMsg = TypedEventMsg | OtherEventMsg

export function isEventKind<K extends KnownEventKind>(
  e: EventMsg,
  kind: K,
): e is EventBase & { kind: K; payload: EventPayloadByKind[K] } {
  return e.kind === kind
}

// ---------------------------------------------------------------- metrics

/** Population aggregate (§17). The same shape nests under by_tier / by_generation. */
export interface MetricsAgg {
  population: number
  mean_balance_usd: number
  median_balance_usd: number
  mean_stress: number
  invalid_action_rate: number
  stale_rate: number
  perseveration_rate: number
  text_coherence_mean: number | null
  action_regret_mean: number | null
  false_claim_rate: number
  degenerate_induced_count: number
  calls: number
  real_cost_usd: number
  world_cost_usd: number
  gossip_transfers: number
}

export interface MetricsRow extends MetricsAgg {
  run_id: string
  seed: number
  config_hash: string
  experiment: string | null
  arm: string | null
  tick: number
  day: number
  by_tier: Record<string, MetricsAgg>
  by_generation: Record<string, MetricsAgg>
}

export interface MetricsMsg {
  type: 'metrics'
  tick: number
  day: number
  row: MetricsRow
}

export interface DayRow extends MetricsAgg {
  day: number
  by_tier: Record<string, MetricsAgg>
  by_generation: Record<string, MetricsAgg>
  notes_by_channel?: Record<string, number>
  gini?: number
}

export interface DayMsg {
  type: 'day'
  day: number
  row: DayRow
}

// ---------------------------------------------------------------- status

export interface StatusMsg {
  type: 'status'
  paused: boolean
  tick_seconds: number
  status: RunStatus
}

export type ServerMsg =
  | HelloMsg
  | RosterMsg
  | SnapshotMsg
  | GadgetsMsg
  | TasksMsg
  | ChronicleMsg
  | EventMsg
  | MetricsMsg
  | DayMsg
  | StatusMsg

export type ServerMsgType = ServerMsg['type']

// ---------------------------------------------------------------- HTTP /api

export interface SessionResponse {
  token: string
}

/** GET /api/events?since_seq=&limit= — the ws `event` messages, `type` optional. */
export interface EventsResponse {
  events: Array<Omit<EventMsg, 'type'> & { type?: 'event' }>
}

/** GET /api/metrics?since_tick= — the ws `metrics` messages, `type` optional. */
export interface MetricsResponse {
  metrics: Array<Omit<MetricsMsg, 'type'> & { type?: 'metrics' }>
}

/** GET /api/tree — lineage as nested roots (alive and archived agents). */
export interface TreeNode {
  id: string
  name: string
  tier: string
  generation: number
  status: AgentStatus
  born_tick: number
  died_tick: number | null
  children: TreeNode[]
}

export interface TreeResponse {
  roots: TreeNode[]
}

export interface SelfVersion {
  version: number
  tick: number
  summary: string
}

export interface NoteRecord {
  note_id: string
  title: string
  body: string
  created_tick: number
  channel: 'observed' | 'gossip' | 'inherited' | 'chronicle' | 'tracer' | string
  hop: number
  importance: number
  archived: boolean
}

export interface CallRecord {
  tick: number
  purpose: 'decide' | 'gossip' | 'chronicle' | string
  real_cost_usd: number
  latency_ms: number
  text_coherence: number | null
  invalid_action: 0 | 1
  degenerate_induced: 0 | 1
}

/** GET /api/agents/{id} — record, self history, notes, calls[last 240], balance series. */
export interface AgentDetail {
  agent: RosterAgent & { balance_usd: number; stress: number; x: number; y: number }
  self_versions: SelfVersion[]
  notes: NoteRecord[]
  calls: CallRecord[]
  balance_series: Array<{ tick: number; balance_usd: number }>
}

export interface CommandAck {
  cmd_id: string
  will_apply_at_tick: number
}

// POST bodies (§15)
export interface PostTaskBody {
  title: string
  description: string
  reward_usd: number
}
export interface EpochBody {
  kind: EpochKind
  duration_days: number
  scarcity?: number
  weather_baseline?: number
  arrival?: { name: string; tier: string; balance_usd: number }
}
export type BenefactorBody = { agent_id?: string; amount_usd: number } | { enabled: boolean }
