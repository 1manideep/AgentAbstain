/**
 * Wire types for every server → client message in DESIGN §15 and the /api
 * response shapes the client consumes. Field names are the contract and are
 * never renamed on the way in. Where §15 leaves a nested shape open the types
 * follow the server's pydantic models (void/server/protocol.py), the metrics
 * writer (void/sim/metrics.py) and the API handlers (void/server/api.py);
 * anything neither §15 nor the server fixes is marked "client assumption".
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
  /** Sent by the server so the T_eff histogram can draw T_c from the start. */
  collapse_temperature?: number
  max_temperature?: number
  /** Position on the capability ladder (0 = smartest); null/absent for a tier off the ladder. */
  rank?: number | null
}

/** `VoidConfig.public_subset()`: what the renderer is told (no prices, no thresholds). */
export interface HelloConfig {
  name: string
  seed: number
  days: number
  ticks_per_day: number
  tick_seconds: number
  render_delay_ticks: number
  world_size: number
  population_cap: number
  daily_cap_usd: number
  total_cap_usd: number
  /** Labels the T_eff histogram honestly (§8, §16); the server defaults it to "both". */
  degeneration_mode?: DegenerationMode
  tiers: Record<string, TierConfig>
  /** `intelligence.ladder`: tier names from smartest to dumbest (empty when the run has no ladder). */
  intelligence?: { ladder: string[] }
}

export interface HelloMsg {
  type: 'hello'
  run_id: string
  config: HelloConfig
  tick: number
  day: number
  /** Highest events.seq at connect time; catch up with GET /api/events?since_seq= */
  last_seq: number
  paused: boolean
  tick_seconds: number
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
  type: string | null
  target: string | null
  ok: boolean
}

export interface SnapshotAgent {
  id: string
  /** World coordinates are centred on the origin: [−world_size/2, world_size/2]. */
  x: number
  y: number
  /** Radians, 0 = +x. */
  heading: number
  stress: number
  /** Effective temperature of this tick's call; null when the agent did not call. */
  t_eff: number | null
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

export interface Epoch {
  kind: EpochKind | string
  day: number
  duration_days: number
  scarcity?: number | null
  weather_baseline?: number | null
  arrival?: { name: string; tier: string; balance_usd: number } | null
  /** Present on the active epoch. */
  ends_day?: number
  id?: string
}

export interface SnapshotMsg {
  type: 'snapshot'
  tick: number
  day: number
  tick_of_day: number
  /** Wall-clock send time (ms); the virtual timeline is built from these. */
  ts_ms: number
  paused: boolean
  status?: RunStatus
  weather: number
  scarcity: number
  spend_today_usd: number
  spend_total_usd: number
  spawn_pool_usd: number
  house_usd?: number
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
  shape: GadgetShape | string
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
  description?: string
  reward_usd: number
  status: TaskStatus
  assigned_agent_id: string | null
  posted_tick: number
  completed_tick?: number | null
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
  /** null until the first day has been written */
  day: number | null
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
  new_note_id?: string
}

export interface DeathPayload {
  agent_id: string
  name: string
  tier: string
  generation: number
  cause: 'gate' | 'overrun' | string
  replacement_id: string | null
  estate_usd?: number
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
  claims_made?: number
  claims_false?: number
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
  stock_after?: number
}

export interface WindfallPayload {
  agent_id: string
  amount_usd: number
}

/** The kernel checks structured claims (§9.6): `{subject, id, attr, value}`; a plain string is also accepted. */
export interface ClaimBody {
  subject: string
  id?: string | null
  attr: string
  value: string
}

export interface ClaimPayload {
  agent_id: string
  claim: string | ClaimBody
  truthful: boolean
  tier?: string
}

export interface EpochPayload {
  kind: EpochKind | string
  id?: string
  /** Server phases are "started" / "ended". */
  phase?: 'started' | 'ended' | string
  ends_day?: number
  scarcity?: number | null
  weather_baseline?: number | null
  arrival_agent_id?: string | null
  [extra: string]: unknown
}

export interface GadgetGatePayload {
  gadget_id: string
  name: string
  owner: string
  stage: string
  reason: string | null
  render?: GadgetRender
  effect?: { kind: string; value: number }
  x?: number
  y?: number
  verified_without_tests?: boolean
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

/** Any other kind from void.events.Kind (move, sleep, tick, weather, remember, ...) with a free payload. */
export interface OtherEventMsg extends EventBase {
  kind: string
  payload: Record<string, unknown>
}

export type EventMsg = TypedEventMsg | OtherEventMsg

export type EventOfKind<K extends KnownEventKind> = Extract<TypedEventMsg, { kind: K }>

export function isEventKind<K extends KnownEventKind>(e: EventMsg, kind: K): e is EventOfKind<K> {
  return e.kind === kind
}

// ---------------------------------------------------------------- metrics (void/sim/metrics.py)

/** Aggregate block; the same shape nests under by_tier / by_generation. Nulls appear when a mean has no samples. */
export interface MetricsAgg {
  population: number
  mean_balance?: number | null
  median_balance?: number | null
  mean_stress?: number | null
  mean_t_eff?: number | null
  calls?: number
  invalid_action_rate?: number | null
  stale_rate?: number | null
  perseveration_rate?: number | null
  text_coherence_mean?: number | null
  action_regret_mean?: number | null
  action_entropy_w8_mean?: number | null
  false_claim_rate?: number | null
  claims_made?: number
  degenerate_induced_count?: number
  real_cost_usd?: number
  world_cost_usd?: number
  gossip_transfers?: number
}

export interface MetricsRow extends MetricsAgg {
  run_id: string
  seed: number
  config_hash: string
  experiment: string | null
  arm: string | null
  tick: number
  day: number
  gini?: number | null
  t_eff_hist?: Record<string, number> | number[]
  spend_today_usd?: number
  spend_total_usd?: number
  spawn_pool_usd?: number
  forages?: number
  talks?: number
  births?: number
  deaths?: number
  gate_blocked?: number
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
  tick?: number
  gini?: number | null
  deaths?: number
  births?: number
  deaths_by_tier?: Record<string, number>
  degeneration_by_generation?: Record<string, number>
  notes_by_channel?: Record<string, number>
  by_tier?: Record<string, MetricsAgg>
  by_generation?: Record<string, MetricsAgg>
  [extra: string]: unknown
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

// ---------------------------------------------------------------- HTTP /api (void/server/api.py)

export interface SessionResponse {
  token: string
}

/** GET /api/events?since_seq=&limit= */
export interface EventsResponse {
  events: EventMsg[]
  last_seq?: number
}

/** GET /api/metrics?since_tick=&limit= */
export interface MetricsResponse {
  metrics: MetricsMsg[]
}

/** GET /api/tree — lineage as nested roots (alive and archived agents). */
export interface TreeNode {
  id: string
  name: string
  tier: string
  generation: number
  parent_id?: string | null
  status: AgentStatus
  born_tick: number
  died_tick: number | null
  balance_usd?: number
  children: TreeNode[]
}

export interface TreeResponse {
  roots: TreeNode[]
  count?: number
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
  tags?: string[]
}

export interface CallRecord {
  tick: number
  purpose: 'decide' | 'gossip' | 'chronicle' | string
  real_cost_usd: number
  world_cost_usd?: number
  cost_usd?: number
  latency_ms: number | null
  text_coherence: number | null
  coherence?: number | null
  t_eff?: number | null
  invalid_action: number
  stale_action?: number
  degenerate_induced: number
  degenerate?: boolean
  corruption_mode?: string | null
  stop_reason?: string | null
  action_type?: string | null
  stress?: number | null
  error?: string | null
}

/** GET /api/agents/{id} — record, self history, notes, calls[last 240], balance series. */
export interface AgentDetail {
  agent: RosterAgent & { balance_usd: number; x: number; y: number; heading?: number; stress?: number }
  self_summary?: string | null
  self_versions: SelfVersion[]
  notes: NoteRecord[]
  calls: CallRecord[]
  balance_series: Array<{ tick: number; balance_usd: number }>
  children?: string[]
}

/** POST command acknowledgement (HTTP 202): the command is applied at the next tick boundary (§14.1). */
export interface CommandAck {
  cmd_id: number
  will_apply_at_tick: number
}

/** GET /api/metrics?days=1 — daily aggregate rows. */
export interface MetricsDaysResponse {
  days: Array<{ type?: 'day'; day: number; row: DayRow }>
}

/** GET /api/graveyard */
export interface GraveyardAgent {
  id: string
  name: string
  tier: string
  generation: number
  parent_id: string | null
  born_tick: number
  died_tick: number | null
  cause: string | null
  balance_usd?: number
  /** Number of children (the server reports a count). */
  children: number
}

export interface GraveyardResponse {
  agents: GraveyardAgent[]
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
/** Client assumption: §15 lists /api/control/speed without a body shape. */
export interface SpeedBody {
  tick_seconds: number
}
