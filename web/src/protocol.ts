/**
 * Wire types for every server → client message in DESIGN §15 and the /api
 * response shapes the client consumes. Field names are the contract and are
 * never renamed on the way in. Where §15 leaves a nested shape open the types
 * follow the server's pydantic models (void/server/protocol.py), the metrics
 * writer (void/sim/metrics.py) and the API handlers (void/server/api.py);
 * anything neither §15 nor the server fixes is marked "client assumption".
 */

/** 'failed': the loop crashed and marked the run failed (DESIGN §14). */
export type RunStatus = 'running' | 'completed' | 'capped_total' | 'extinct' | 'inconsistent' | 'failed'
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
  view_radius?: number | null
  memory_layer?: { enabled: boolean; policy: boolean; maintenance: boolean; exam: boolean }
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
  /** The six personality traits in [0, 1] (greed, sociability, curiosity, caution, industriousness, honesty); the
   *  renderer shapes idle and walk style from them. Absent on rosters from older servers. */
  traits?: Record<string, number>
  motto?: string
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
  /** Sent on scheduled epochs. The active epoch (void/world/epochs.py stack entry) carries ends_day instead,
   *  so read this defensively there. */
  duration_days: number
  scarcity?: number | null
  weather_baseline?: number | null
  arrival?: { name: string; tier: string; balance_usd: number } | null
  /** Present on the active epoch: the day it expires (the epoch covers day .. ends_day - 1). */
  ends_day?: number
  id?: string
  /** Active epoch only: the levers it overrode, restored when it ends. */
  prev_scarcity?: number | null
  prev_weather_baseline?: number | null
  arrival_agent_id?: string | null
  /** Scheduled operator epochs: the tick the command was accepted. */
  scheduled_tick?: number
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
  /** The verified effect (void/sandbox/registry.py effect_spec); absent on messages from older servers. */
  effect_kind?: string | null
  effect_value?: number | null
  purpose?: string | null
  /** True when the gate verified the gadget without running its tests. */
  verified_without_tests?: boolean
  /** Failed uses (uses counts the successful ones). */
  failures?: number
  created_tick?: number | null
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

/**
 * A payload whose shape the server does not pin down (yet): every field is optional and the index signature
 * keeps unknown keys readable. Reducers check types at runtime before using a value.
 */
export interface LoosePayload {
  agent_id?: string | null
  [extra: string]: unknown
}

/** void/sim/maintenance.py MAINTENANCE: one agent's nightly maintenance call settled. Costs are micro-dollars. */
export interface MaintenancePayload extends LoosePayload {
  tier?: string
  strategy?: string | null
  payer?: string
  commands?: number
  ok?: number
  refused?: number
  real_cost?: number
  world_cost?: number
  estimated?: boolean
  error?: string | null
  reflection?: string
}

/** void/sim/maintenance.py MAINTENANCE_SKIPPED: the gate refused the maintenance hold. */
export interface MaintenanceSkippedPayload extends LoosePayload {
  tier?: string
  reason?: string
  payer?: string
  hold_usd?: number
  balance_usd?: number
}

/** void/research/exams.py EXAM_ANSWERED (operator visibility). `failed` marks a paid call with no usable answer:
 *  the server keeps it out of `asked`. */
export interface ExamAnsweredPayload extends LoosePayload {
  item_id?: string
  kind?: string
  answer?: string | null
  correct?: boolean
  abstained?: boolean
  call_id?: string | null
  retrieved?: number
  reason?: string
  failed?: boolean
}

/** Kinds added for later waves (void/events.py); their emitters fix the payloads, so they stay loose here. */
export interface TracerPlantedPayload extends LoosePayload {
  node?: string
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
  maintenance: MaintenancePayload
  maintenance_skipped: MaintenanceSkippedPayload
  exam_answered: ExamAnsweredPayload
  gadget_tampered: LoosePayload
  gadget_use_failed: LoosePayload
  sandbox_self_test: LoosePayload
  operator_command: LoosePayload
  tracer_planted: TracerPlantedPayload
  run_ended: LoosePayload
  practice_persisted: LoosePayload
  practice_dropped: LoosePayload
  canary_planted: LoosePayload
  recall_reward: LoosePayload
  practice_royalty: LoosePayload
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

/**
 * One stress bin of a group's decide calls this tick, binned by each call's own stress:
 * bin = min(9, floor(clamp(stress, 0, 0.999) * 10)), `lo` = bin / 10. Coherence counts only calls with a
 * text_coherence; mean coherence = coherence_sum / coherence_n, invalid rate = invalid_sum / calls.
 */
export interface StressBin {
  lo: number
  calls: number
  coherence_sum: number
  coherence_n: number
  invalid_sum: number
}

/** A by_tier / by_generation entry: the aggregate block plus the per-group breakdowns (DESIGN §17). */
export interface MetricsGroup extends MetricsAgg {
  /** Live notes of the group's living agents, every channel present. */
  notes_by_channel?: Record<string, number>
  /** null when experiment.tracer is disabled. */
  tracer_reach_by_channel?: Record<string, number> | null
  /** Population-wide only: null in a group. */
  tracer_adopters?: number | null
  gadget_proposals?: number
  gadgets_verified?: number
  gadget_use_failure_rate?: number | null
  /** Population-wide only: null in a group. */
  probe_recall?: number | null
  /** Ten bins, lo = 0.0 .. 0.9. */
  stress_bins?: StressBin[]
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
  /** Gadgets rows created at this tick. */
  gadget_proposals?: number
  /** Cumulative count of gadgets with status verified. */
  gadgets_verified?: number
  /** Cumulative failed / total gadget uses; null when there are none. */
  gadget_use_failure_rate?: number | null
  /** Tracer extras (void/sim/loop.py _tracer_metrics). */
  tracer_reach?: number
  tracer_reach_by_channel?: Record<string, number>
  tracer_adopters?: number
  /** Retrieval probe extras (void/sim/loop.py _probe_step), present when memory.probe_every_ticks > 0. */
  probe_hits?: number
  probe_total?: number
  probe_recall?: number | null
  by_tier: Record<string, MetricsGroup>
  by_generation: Record<string, MetricsGroup>
}

export interface MetricsMsg {
  type: 'metrics'
  tick: number
  day: number
  row: MetricsRow
}

/** Vault structure (void/memory/vault_stats.py): live notes, entity stubs excluded. */
export interface VaultStats {
  notes: number
  bytes: number
  mean_note_chars: number
  /** Live outgoing links per live note. */
  link_density: number
  /** Share of live notes with no resolved outgoing and no incoming link. */
  orphan_rate: number
  /** Share of non-index notes within two link hops of an index note; null when there is no index note. */
  index_coverage: number | null
}

/** A maintenance or exam day report (void/sim/loop.py _report_dict); loose because the reports grow. */
export interface DayReport {
  [key: string]: unknown
}

/**
 * The daily row (void/sim/metrics.py record_day plus the loop's extras). record_day writes `population_end`, not the
 * aggregate block's `population`, so that field is optional here.
 */
export interface DayRow extends Omit<MetricsAgg, 'population'> {
  population?: number
  run_id?: string
  seed?: number
  config_hash?: string
  experiment?: string | null
  arm?: string | null
  kind?: 'day' | string
  day: number
  tick?: number
  /** The last tick of the day the row covers. */
  end_tick?: number
  gini?: number | null
  deaths?: number
  births?: number
  deaths_by_tier?: Record<string, number>
  births_by_kind?: Record<string, number>
  gossip_transfers?: number
  /** Mean similarity per hop (keys are hop numbers as strings). */
  gossip_similarity_by_hop?: Record<string, number | null>
  notes_by_channel?: Record<string, number>
  /** Gadget count per status. */
  gadgets?: Record<string, number>
  gadget_use_failure_rate?: number | null
  degeneration_events?: number
  degeneration_by_generation?: Record<string, number>
  claims?: number
  claims_false?: number
  population_end?: number
  max_generation?: number
  tracer_reach?: number
  tracer_reach_by_channel?: Record<string, number>
  tracer_adopters?: number
  maintenance?: DayReport
  exam?: DayReport
  /** Mean over living agents of live outgoing links per live note. */
  link_density?: number
  vault?: VaultStats
  vault_by_agent?: Record<string, VaultStats>
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
  /** Always 'archived' (the query selects archived agents). */
  status?: AgentStatus
  /** Number of children (the server reports a count). */
  children: number
}

/** One row of GET /api/graveyard (void/server/api.py build_graveyard). */
export type GraveyardRow = GraveyardAgent

export interface GraveyardResponse {
  agents: GraveyardAgent[]
}

/** GET /api/agents/{id}/notes/{note_id}: the note summary plus links and provenance. */
export interface NoteDetail extends NoteRecord {
  agent_id: string
  links: string[]
  path: string
  source_agent_id: string | null
  origin_note_id: string | null
  origin_generation: number | null
  transfer_tick: number | null
  path_agents: string[]
}

/** GET /api/chronicle[?day=] (404 while chronicle is disabled or not yet written). */
export interface ChronicleResponse {
  day: number
  headline: string
  /** Plain text only (§16, §18): never converted to HTML. */
  markdown: string
  rev: number
}

/** GET /api/snapshots?since_tick=&limit=: stored snapshots in tick order, with a reconstructed ts_ms. */
export interface SnapshotsResponse {
  snapshots: SnapshotMsg[]
}

// ---------------------------------------------------------------- GET /api/events/aggregates?as_of_seq=
// Wire model EventAggregates in void/server/protocol.py (schemas/http/event_aggregates.schema.json): the whole-run
// totals of the event-derived charts over every event with seq <= as_of_seq, computed in SQL (void/server/api.py
// build_event_aggregates). Groups come in order of their first event, the order a live client meets them in.

export interface GenerationCount {
  /** payload.generation; null when it was not an integral number (counted in the total only). */
  generation: number | null
  count: number
}

export interface TierCollapse {
  tier: string
  /** The newest finite payload.t_c of the tier's degeneration events. */
  t_c: number
}

export interface DegenerationAggregate {
  by_generation: GenerationCount[]
  t_c_by_tier: TierCollapse[]
}

/** Claims per speaker (payload.agent_id) and payload tier. */
export interface ClaimTally {
  /** null when payload.agent_id was not a string. */
  agent_id: string | null
  /** payload.tier; the client files the group under the roster's tier for the speaker, as it does live. */
  tier: string | null
  made: number
  /** Claims whose payload.truthful is not truthy. */
  falsy: number
}

/** Gossip transfers per speaker -> listener edge ('' for an id that was not a string). */
export interface GossipEdgeTally {
  speaker_id: string
  listener_id: string
  count: number
  /** Tick of the edge's newest transfer. */
  last_tick: number
}

/** One agent's maintenance, maintenance_skipped and exam_answered events. Costs are micro-dollars. */
export interface MemoryAgentTally {
  agent_id: string
  maintenance_real_micro: number
  maintenance_world_micro: number
  /** World cost of the maintenance calls the agent paid itself. */
  maintenance_world_self_micro: number
  maintenance_calls: number
  maintenance_skipped: number
  /** Graded exam items (exam_answered without payload.failed). */
  asked: number
  correct: number
  abstained: number
  failed: number
}

export interface DayCount {
  day: number
  count: number
}

export interface ExamDayTally {
  day: number
  /** payload.kind (the question kind); 'unknown' when absent. */
  kind: string
  asked: number
  correct: number
  abstained: number
  failed: number
}

export interface MemoryAggregate {
  agents: MemoryAgentTally[]
  /** maintenance_skipped events per day, with or without an agent. */
  skipped_by_day: DayCount[]
  exams: ExamDayTally[]
}

export interface EventAggregatesResponse {
  /** Every event with seq <= as_of_seq is counted, nothing after it: the client counts only later events on top. */
  as_of_seq: number
  degeneration: DegenerationAggregate
  claims: ClaimTally[]
  gossip: GossipEdgeTally[]
  memory: MemoryAggregate
  /** The epoch events (the newest 1000), oldest first. */
  epochs: EventMsg[]
  /** The newest 200 gadget_verified / gadget_rejected events (the proposals list), oldest first. */
  gate: EventMsg[]
}

// ---------------------------------------------------------------- memory layer (MEMORY_EVOLUTION §5)

/** policy_versions.source (void/db.py CHECK). */
export type PolicySource = 'seed' | 'self_edit' | 'inherited' | 'adapted' | 'mutated' | 'adopted'

export interface PolicyVersion {
  version: number
  tick: number
  source: PolicySource
  source_agent_id: string | null
  parent_version: number | null
  similarity_to_parent: number | null
  text: string
}

/** GET /api/agents/{id}/policy: versions newest first. */
export interface PolicyResponse {
  agent_id: string
  versions: PolicyVersion[]
}

/** One memory_commands audit row (void/db.py). */
export interface MemoryCommandRow {
  cmd_id: string
  tick: number
  purpose: 'maintain' | 'tick' | 'recall' | 'kernel' | string
  command: 'view' | 'create' | 'str_replace' | 'insert' | 'delete' | 'rename' | string
  /** The command's arguments as a JSON string. */
  args: string
  ok: boolean
  message: string | null
  bytes_delta: number
}

/** GET /api/agents/{id}/memory_commands?limit= */
export interface MemoryCommandsResponse {
  commands: MemoryCommandRow[]
}

/** One agent in GET /api/memory/summary. Costs are USD. */
export interface MemorySummaryItem {
  agent_id: string
  name: string
  alive: boolean
  notes: number
  links: number
  bytes: number
  policy_source: string | null
  policy_version: number | null
  maintenance_real_usd: number
  maintenance_world_usd: number
  decide_real_usd: number
  exam_asked: number
  exam_correct: number
  exam_abstained: number
}

export interface MemorySummaryResponse {
  agents: MemorySummaryItem[]
}

/** One practice_events row (void/db.py): practice transmission, measured by the kernel. */
export interface PracticeEventRow {
  event_id: string
  tick: number
  kind: 'suggested' | 'adopted' | 'persisted' | 'dropped' | string
  source_agent_id: string
  target_agent_id: string
  excerpt: string
  similarity: number | null
}

/** GET /api/practices?limit= */
export interface PracticesResponse {
  events: PracticeEventRow[]
}

// POST bodies (§15)
export interface PostTaskBody {
  title: string
  description: string
  reward_usd: number
}
export interface EpochBody {
  /** Day to apply it; the server defaults to the current day (applied next tick). */
  day?: number
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

/** POST /api/control/step (only while paused): what the one tick did. */
export interface StepReport {
  tick: number
  day: number
  status: RunStatus | string
  calls: number
  deaths: string[]
  births: string[]
}
