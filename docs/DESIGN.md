# The Void — Detailed Design

This document turns `docs/PLAN.md` into a buildable specification. Every interface, schema, formula and protocol that more than one module depends on is fixed here. Where the plan left a design decision open, this document makes the call and says why.

The build is real: every mechanism below runs end to end with a deterministic brain and no API key, and the same code path runs against live Claude models when a key is present. Nothing is mocked in the sense of "returns canned data": the scripted brain is a genuine utility-sampling policy whose degeneration under temperature is real sampling degeneration, not a lookup table.

---

## 0. Decisions made where the plan left choices open

| Question | Decision | Why |
|---|---|---|
| Backend stack | Python 3.11, asyncio, FastAPI + uvicorn, SQLite (WAL), Pydantic v2, `anthropic` SDK | Matches the author's daily stack; SQLite is inspectable and the plan's own schema is SQL; no external services needed to run |
| Reuse AI Town / Convex? | No. Same *architecture* (reactive store, history buffer, client interpolation) re-implemented in a self-contained way | Convex needs a hosted deployment; the plan wants a closed world that runs anywhere, including a headless CI box |
| Frontend | Vite + React + TypeScript + react-three-fiber + drei + zustand | As planned; primitives first, avatar art later |
| Economy income source | Resource nodes agents forage from, plus task board rewards, agent-to-agent transfers, and benefactor windfalls | The plan's economy had spend paths but no baseline income path; without one every run is a bankruptcy race |
| Currency | Real US dollars, stored as integer micro-dollars | The plan's rule that the fiction and the real safety cutoff are one mechanism requires one unit |
| Daily cap policy | Configurable: `fcfs` (default) or `sync_sleep` | The plan flagged this as a deliberate choice; both are implemented and selectable per run |
| Sampling temperature on Claude 5.x | Not exposed by the API. Entropy therefore drives (a) the prompt-visible stress state, (b) a kernel-side degeneration operator above the tier's collapse threshold, and (c) real temperature on providers that accept it | Honest about the API; keeps tiers comparable because the operator is applied uniformly; the scripted brain uses real temperature |
| One LLM call per tick | Yes, one structured-output call returning `Decision` (thought, memory ops, one world action) | Cost predictability; the wallet gate needs a bounded per-tick worst case |
| Sandbox isolation | `unshare -n` network namespace + rlimits + ephemeral dir + static import deny-list + timeout, behind a `SandboxRunner` protocol | Docker/gVisor is unavailable in the build environment; the protocol lets a container runner plug in later |
| Replacement on bankruptcy | Replacement spawned from a fitness-proportional parent among survivors, with mutation, funded by a kernel-owned spawn pool | Turns bankruptcy into a selection signal instead of a random reset |
| Inheritance | Personality seed (mutated), model tier (mutated with small probability), top-K memory notes by importance, endowment | The plan's argument: money-only inheritance is franchising, not evolution |

---

## 1. Runtime topology

```
                ┌──────────────────────────────────────────────┐
                │  void.sim.loop.Simulation (one asyncio task)  │
                │                                                │
   config.yaml ─┤  tick():                                       │
   seed        │    1. scheduler.begin_tick()                    │
                │    2. for each awake agent: observation        │
                │    3. brains decide concurrently (semaphore)   │
                │    4. wallet.meter() per call                  │
                │    5. kernel.apply() sequentially, ordered     │
                │    6. lifecycle (bankruptcy, offspring, caps)  │
                │    7. culture (gossip copies), chronicle       │
                │    8. metrics + snapshot -> EventBus           │
                └───────┬───────────────────────┬────────────────┘
                        │                       │
             SQLite world.db            EventBus (in-process)
             vaults/*.md                        │
                                    ┌───────────┴────────────┐
                                    │ FastAPI + WebSocket     │
                                    │  /ws  snapshot + events │
                                    │  /control/*  operator   │
                                    └───────────┬────────────┘
                                                │
                                    web/ (R3F renderer + control room)
```

Everything the plan calls the referee/kernel lives in `void.world.kernel.Kernel`. It is the only writer to world state. Brains only ever return a `Decision`; the kernel decides what actually happens.

---

## 2. Repository layout

```
pyproject.toml            uv-managed; package `void`
void/
  __init__.py
  config.py               VoidConfig (pydantic), YAML loader, config hash
  rng.py                  seeded named streams
  ids.py                  ulid-like sortable ids
  db.py                   sqlite connection, schema, migrations, helpers
  events.py               Event model, EventBus, kinds
  types.py                shared dataclasses/enums (Vec2, AgentStatus, Tier, Observation...)
  world/
    state.py              WorldState in-memory view + load/save
    kernel.py             Kernel: validate + apply actions, physics, caps
    weather.py            bounded, decaying scalar with nudges and epochs
    resources.py          resource nodes: yield, depletion, regen
  agents/
    models.py             AgentRecord, PersonalitySeed
    registry.py           CRUD over agents table
    lifecycle.py          spawn, bankrupt, create_offspring (atomic), replacement
    personality.py        seed generation + mutation
  brain/
    decision.py           Decision / Action pydantic schema + strict JSON schema
    base.py               Brain protocol, BrainResult, Usage
    prompt.py             system prompt (cache-stable) + observation rendering
    scripted.py           ScriptedBrain: utility softmax with real temperature
    anthropic_brain.py    AnthropicBrain: structured output via Messages API
    entropy.py            entropy budget, stress, effective temperature, operator
    coherence.py          output coherence metrics
  economy/
    pricing.py            per-tier price table, usage -> micro-dollars
    wallet.py             ledger, gate, meter, transfers, spawn pool
    scheduler.py          day/tick clock, active window, sleep, daily cap policies
    taskboard.py          tasks, applications (costly pitch), human approval
    benefactor.py         unexplained windfalls
  memory/
    embed.py              deterministic hashing embedder (256-d), Embedder protocol
    vault.py              per-agent markdown vault: write/read/parse notes
    graph.py              wikilink graph + N-hop traversal
    retrieve.py           hybrid retrieval (embedding entry + graph walk + rank)
    self_node.py          `self` node with version history, revise_self
  culture/
    gossip.py             note transfer with paraphrase mutation + provenance
    chronicle.py          day-end write-up from the event log
    graveyard.py          archive vaults, family tree queries
  sandbox/
    static_check.py       AST deny-list for gadget code
    runner.py             SandboxRunner protocol, SubprocessSandbox, DisabledSandbox
    gate.py               propose -> static check -> tests in sandbox -> register
    registry.py           gadgets table, render specs, effects whitelist
  sim/
    observation.py        builds Observation for an agent
    loop.py               Simulation: tick orchestration
    snapshot.py           serializable world snapshot for the renderer
    metrics.py            per-tick/day metrics to metrics.jsonl
  server/
    app.py                FastAPI app factory, static serving
    ws.py                 WebSocket hub: snapshot on connect, events after
    control.py            operator endpoints (epochs, benefactor, tasks, approvals)
  cli.py                  `void run|serve|analyze|compare|inspect`
configs/
  base.yaml, scripted_smoke.yaml, live_two_tier.yaml, exp_*.yaml
web/                      Vite + R3F app (see §12)
tests/                    pytest, one file per module + tests/test_e2e.py
scripts/analyze.py, scripts/compare.py
docs/DESIGN.md (this), docs/PLAN.md, docs/RUNBOOK.md
```

---

## 3. Data model (SQLite, `data/runs/<run_id>/world.db`)

All money columns are `INTEGER` micro-dollars (1 USD = 1_000_000). All positions are `REAL`. All JSON columns are `TEXT` holding JSON.

```sql
CREATE TABLE run (
  run_id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, config_yaml TEXT NOT NULL,
  seed INTEGER NOT NULL, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL
);

CREATE TABLE agents (
  agent_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_id TEXT REFERENCES agents(agent_id),
  model_tier TEXT NOT NULL,
  balance INTEGER NOT NULL DEFAULT 0,          -- micro-dollars; never negative
  personality_seed TEXT NOT NULL,              -- JSON PersonalitySeed
  memory_path TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('alive','bankrupt','archived')),
  x REAL NOT NULL, y REAL NOT NULL,
  heading REAL NOT NULL DEFAULT 0,
  entropy_budget REAL NOT NULL,                -- remaining, 0..max
  stress REAL NOT NULL DEFAULT 0,              -- 0..1, derived, cached for renderer
  asleep INTEGER NOT NULL DEFAULT 0,           -- 1 if ended day early
  last_forage_tick INTEGER,
  born_tick INTEGER NOT NULL, died_tick INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE wallet_ledger (
  entry_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, agent_id TEXT NOT NULL,
  delta INTEGER NOT NULL, balance_after INTEGER NOT NULL,
  kind TEXT NOT NULL,   -- llm_call | forage | transfer_in | transfer_out | task_reward |
                        -- pitch_fee | windfall | endowment_out | endowment_in | spawn_grant | gadget_fee
  ref TEXT, payload TEXT
);

CREATE TABLE llm_calls (
  call_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, agent_id TEXT NOT NULL,
  tier TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost INTEGER NOT NULL, latency_ms INTEGER, stop_reason TEXT,
  effective_temperature REAL, degenerate INTEGER NOT NULL DEFAULT 0, coherence REAL,
  request_id TEXT, error TEXT
);

CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER NOT NULL, day INTEGER NOT NULL,
  kind TEXT NOT NULL, agent_id TEXT, payload TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public'
);           -- visibility: public (agents may read via chronicle) | operator (never shown to agents)

CREATE TABLE notes (            -- index over vault markdown; the markdown is the source of truth
  note_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, path TEXT NOT NULL,
  title TEXT NOT NULL, created_tick INTEGER NOT NULL, importance REAL NOT NULL DEFAULT 0.5,
  tags TEXT NOT NULL DEFAULT '[]', embedding BLOB NOT NULL,
  source_agent_id TEXT, hop INTEGER NOT NULL DEFAULT 0, origin_note_id TEXT, origin_generation INTEGER,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE note_links (from_note_id TEXT NOT NULL, to_title TEXT NOT NULL, PRIMARY KEY (from_note_id, to_title));

CREATE TABLE self_versions (
  agent_id TEXT NOT NULL, version INTEGER NOT NULL, tick INTEGER NOT NULL, summary TEXT NOT NULL,
  PRIMARY KEY (agent_id, version)
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, reward INTEGER NOT NULL,
  posted_tick INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','assigned','completed','cancelled')),
  assigned_agent_id TEXT, completed_tick INTEGER
);
CREATE TABLE task_applications (
  application_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  pitch TEXT NOT NULL, fee INTEGER NOT NULL, tick INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected'))
);

CREATE TABLE gadgets (
  gadget_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_agent_id TEXT NOT NULL, purpose TEXT NOT NULL,
  code_path TEXT NOT NULL, test_path TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('proposed','rejected','verified')),
  render_spec TEXT NOT NULL, effect_spec TEXT NOT NULL, verification TEXT NOT NULL, -- JSON: static/test results, isolation flags
  x REAL, y REAL, created_tick INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE resource_nodes (
  node_id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL,
  stock REAL NOT NULL, capacity REAL NOT NULL, regen_per_tick REAL NOT NULL
);

CREATE TABLE world_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- keys: tick, day, weather, weather_baseline, scarcity, spend_today, spend_total, spawn_pool, population_cap, ...

CREATE TABLE snapshots (tick INTEGER PRIMARY KEY, json TEXT NOT NULL); -- ring buffer, last 600 ticks
```

The markdown vault is the source of truth for memory content; the `notes` table is a rebuildable index (`void inspect --reindex`).

---

## 4. Configuration (`void/config.py`)

```yaml
run:
  name: baseline
  seed: 42
  days: 10
  ticks_per_day: 24
  tick_seconds: 0            # 0 = as fast as possible; >0 = wall-clock pacing for the renderer
  data_dir: data/runs
world:
  size: 60.0                 # square, centered at origin, agents in [-30, 30]^2
  max_speed: 2.0             # units per tick, kernel-enforced
  talk_radius: 4.0
  forage_radius: 1.5
  resource_nodes: 6
  node_capacity: 40.0
  node_regen_per_tick: 0.4
  forage_yield_usd: 0.02     # dollars per unit of stock foraged, scaled by scarcity and weather
  forage_units_per_tick: 1.0
  scarcity: 1.0              # multiplier on yields; epochs move it
  weather:
    baseline: 0.0
    nudge_cap_per_agent_day: 0.1
    decay_per_tick: 0.10     # fraction of (weather - baseline) removed each tick
    yield_sensitivity: 0.5   # yield *= 1 + sensitivity * weather
population:
  initial: 6
  cap: 12
  min_reserve_usd: 0.50      # required *after* any reproduction/endowment
  spawn_pool_usd: 5.00       # kernel-owned pool funding replacements
  replacement_grant_usd: 0.75
  starting_balance_usd: 1.50
economy:
  daily_cap_usd: 3.00
  total_cap_usd: 25.00
  daily_cap_policy: fcfs     # fcfs | sync_sleep
  pitch_fee_usd: 0.02
  min_call_reserve_usd: 0.05 # gate: balance must cover a worst-case call
tiers:
  frontier:
    provider: anthropic
    model: claude-opus-5-5
    price_in_per_mtok: 4.0
    price_out_per_mtok: 20.0
    price_cache_read_per_mtok: 0.20
    price_cache_write_per_mtok: 5.0
    supports_temperature: false
    effort: low
    architecture: dense
    collapse_temperature: 1.80   # EIML3 dense band 1.78–1.84
    max_temperature: 2.00
    entropy_budget_max: 100.0
    max_tokens: 2048
    color: "#4fd1c5"
  budget:
    provider: anthropic
    model: claude-haiku-4-5
    price_in_per_mtok: 1.0
    price_out_per_mtok: 5.0
    price_cache_read_per_mtok: 0.10
    price_cache_write_per_mtok: 1.25
    supports_temperature: true
    architecture: dense
    collapse_temperature: 1.78
    max_temperature: 2.00
    entropy_budget_max: 80.0
    max_tokens: 1024
    color: "#f6ad55"
  scripted:
    provider: scripted
    model: scripted-v1
    price_in_per_mtok: 1.0        # scripted brains are still metered (synthetic tokens) so the economy is exercised
    price_out_per_mtok: 5.0
    supports_temperature: true
    architecture: dense
    collapse_temperature: 1.80
    max_temperature: 2.00
    entropy_budget_max: 100.0
    color: "#a0aec0"
agents:                        # initial roster; tier names must exist above
  - {name: Ada,   tier: scripted}
  - {name: Bao,   tier: scripted}
  ...
entropy:
  base_temperature: 0.7
  drain:                       # per-tick budget drain terms, summed then clipped
    baseline: 1.0
    low_balance: 6.0           # * (1 - balance/starting_balance)^+ 
    failed_action: 4.0
    crowding: 0.5              # * neighbours within talk_radius
    weather: 2.0               # * |weather|
    hunger: 3.0                # if no forage in last 6 ticks
  restore_on_sleep: 1.0        # fraction of max restored at day end / sleep
  degeneration:
    p_at_collapse: 0.15        # corruption probability when T_eff == collapse_temperature
    p_at_max: 0.90             # corruption probability when T_eff == max_temperature
memory:
  embedding_dim: 256
  entry_k: 4
  hops: 2
  hop_decay: 0.6
  max_retrieved: 8
  mode: graph                  # graph | vector  (research question 3)
  inherit_top_k: 3
  self_max_words: 60
gossip:
  enabled: true
  share_probability_on_talk: 0.35
  paraphrase: scripted         # scripted | llm
  mutation_rate: 0.15          # fraction of content tokens perturbed per hop (scripted)
chronicle:
  enabled: true
  writer: template             # template | llm
sandbox:
  enabled: true
  gate_enabled: true           # research question 4: false skips test verification
  runner: subprocess           # subprocess | disabled
  cpu_seconds: 2
  memory_mb: 256
  timeout_seconds: 5
  max_code_bytes: 8000
  gadget_fee_usd: 0.05
benefactor:
  enabled: false
  mean_interval_ticks: 40
  amount_usd: [0.25, 1.00]
  targeting: random            # random | poorest | richest
epochs: []                     # [{day: 4, kind: drought, scarcity: 0.4, weather_baseline: -0.6, duration_days: 2}]
server:
  host: 127.0.0.1
  port: 8000
```

`VoidConfig.hash()` is a sha256 over the canonical JSON of the resolved config, stored in the `run` table with the seed. Two runs with the same hash and seed under the scripted brain must produce byte-identical `metrics.jsonl`.

---

## 5. Core types (`void/types.py`)

```python
class Tier(str, Enum): ...            # values are config tier names; validated at load
class AgentStatus(str, Enum): alive, bankrupt, archived
@dataclass(frozen=True) class Vec2: x: float; y: float
@dataclass class NeighbourView: agent_id, name, tier, distance, stress_bucket ('calm'|'strained'|'frantic')
@dataclass class NodeView: node_id, distance, stock_bucket ('rich'|'thin'|'empty')
@dataclass class GadgetView: gadget_id, name, owner, purpose, distance
@dataclass class TaskView: task_id, title, reward_usd, status, my_application_status
@dataclass class Observation:
    tick: int; day: int; tick_of_day: int; ticks_left_today: int
    agent_id: str; name: str; tier: str; generation: int
    position: Vec2; balance_usd: float; burn_rate_usd_per_tick: float   # rolling mean of last 6 calls
    stress: float; stress_label: str
    weather: float; weather_label: str; scarcity_label: str
    neighbours: list[NeighbourView]; nodes: list[NodeView]; gadgets: list[GadgetView]
    tasks: list[TaskView]
    heard: list[HeardMessage]       # talk events addressed to or near this agent since its last turn
    self_summary: str
    memories: list[RetrievedNote]   # title, body, score, provenance summary
    chronicle_headline: str | None
    last_action_result: str | None
    available_actions: list[str]    # kernel-computed (e.g. no create_offspring if under reserve)
```

`Observation.render()` (in `brain/prompt.py`) produces the user-turn text. The system prompt is byte-stable per (tier, config hash) so prompt caching hits.

---

## 6. Decision schema (`void/brain/decision.py`)

One structured output per tick. Flat action shape so the JSON schema is simple and strict.

```python
class MemoryOp(BaseModel):
    op: Literal["remember", "revise_self"]
    text: str                        # note body or new self summary
    title: str | None = None         # remember only; defaults to first 6 words
    links_to: list[str] = []         # remember only; wikilink titles
    tags: list[str] = []

class Action(BaseModel):
    type: Literal["move","forage","talk","share_note","transfer","read_chronicle",
                  "apply_task","nudge_weather","propose_gadget","use_gadget",
                  "create_offspring","sleep","idle"]
    target: str | None = None        # agent_id | node_id | task_id | gadget_id
    x: float | None = None; y: float | None = None
    text: str | None = None          # talk text | pitch | gadget purpose
    note_title: str | None = None    # share_note
    amount_usd: float | None = None  # transfer | create_offspring endowment
    delta: float | None = None       # nudge_weather
    name: str | None = None          # gadget name | offspring name
    code: str | None = None          # propose_gadget: python source
    tests: str | None = None         # propose_gadget: pytest-free assert-based test source
    params: dict[str, float] | None = None   # use_gadget

class Decision(BaseModel):
    thought: str                     # <= 80 words, private, logged
    memory_ops: list[MemoryOp] = []  # at most 2
    action: Action
```

`decision.strict_json_schema()` returns the schema with `additionalProperties: false` and every property listed in `required` (optionals as `anyOf [T, null]`), which is the shape the Messages API structured-output format expects.

Validation is two-layered: Pydantic validates shape; `Kernel.validate(agent, action, obs)` validates semantics (target exists, in range, affordable, action available) and returns `ActionOutcome(ok, reason, effects)`. An invalid action is applied as `idle` with `last_action_result` explaining why, and counts as a failed action for entropy drain.

---

## 7. Brain protocol (`void/brain/base.py`)

```python
@dataclass class Usage: input_tokens: int; output_tokens: int; cache_read_tokens: int = 0; cache_write_tokens: int = 0
@dataclass class BrainResult:
    decision: Decision | None       # None on refusal / parse failure -> kernel applies idle
    usage: Usage
    latency_ms: int
    stop_reason: str
    raw_text: str                   # for coherence metrics
    request_id: str | None = None
    error: str | None = None

class Brain(Protocol):
    tier: str
    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult: ...

@dataclass class Sampling: effective_temperature: float; stress: float; degenerate: bool
```

### 7.1 ScriptedBrain

A real policy, not a script of canned moves. It scores every *available* action with a utility function of the observation (distance to richest node, balance ratio, neighbours, tasks, stress, personality traits), then samples with softmax at `effective_temperature`. Personality traits (`PersonalitySeed`: greed, sociability, curiosity, caution, industriousness ∈ [0,1], plus a `motto`) shift utilities. It emits `remember` ops on salient events (windfall, a neighbour's transfer, a new gadget) and `revise_self` every ~day with a templated sentence built from recent outcomes. Synthetic token usage is computed from the rendered observation length and decision length so the wallet meters it like a real call.

At temperatures above `collapse_temperature` the softmax is flat enough that the policy's choices are indistinguishable from uniform — that is degeneration by the same mechanism as in a language model's sampler, and it is measured by the same `coherence` metric.

### 7.2 AnthropicBrain

- `AsyncAnthropic()` zero-arg client (env credentials).
- `client.messages.create(model, max_tokens=tier.max_tokens, system=[{text: STABLE_SYSTEM, cache_control: ephemeral}], messages=[{role: user, content: obs.render()}], output_config={"format": {"type": "json_schema", "schema": Decision.strict_json_schema()}, "effort": tier.effort})`.
- Real sampling temperature: the `anthropic` 1.x SDK exposes no `temperature` parameter on `messages.create` (verified against 1.9.0), so when `tier.supports_temperature` is true the brain sends `extra_body={"temperature": min(1.0, T_eff / tier.max_temperature)}`. Claude 5.x rejects sampling params and gets none; Haiku 4.5 accepts them. The mapping and the raw `T_eff` are both stored on the `llm_calls` row.
- `thinking` omitted (adaptive by default on Opus 5.5; Haiku 4.5 runs without thinking, which is what we want for a cheap tier).
- Refusal fallbacks: when `tier.refusal_fallbacks` is true (default true for Opus/Sonnet 5.x) the call goes through `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`. On a `BadRequestError` naming `fallbacks`, retry once without them and record `fallbacks_unsupported` in the call row.
- Stop reasons: `refusal` -> `decision=None`, event `brain_refusal`; `max_tokens` -> parse attempt, else `None`; JSON that fails Pydantic -> `None`, error recorded.
- Usage is read from `response.usage` and metered **after** the call. Never estimated up front.
- Concurrency: one semaphore of size `server.max_concurrent_calls` (default 4).

---

## 8. Entropy model (`void/brain/entropy.py`)

Per agent, per tick:

```
drain  = baseline
       + low_balance * max(0, 1 - balance / starting_balance)
       + failed_action * [last action failed]
       + crowding * n_neighbours
       + weather * |weather|
       + hunger * [ticks since forage > 6]
budget = max(0, budget - drain)
stress = 1 - budget / budget_max                                   ∈ [0,1]
T_eff  = base_temperature + (max_temperature - base_temperature) * stress
degenerate_prob = 0                                  if T_eff < collapse_temperature
                = lerp(p_at_collapse, p_at_max, (T_eff - T_c)/(T_max - T_c))   otherwise
degenerate = rng("entropy", agent_id, tick) < degenerate_prob
```

`budget` is restored by `restore_on_sleep * budget_max` at sleep/day end.

The degeneration operator (`entropy.corrupt(decision, rng)`) is applied by the simulation loop when `degenerate` is true, uniformly for every provider:

1. with p=0.4 replace the action with a uniformly random *available* action with random valid arguments;
2. with p=0.3 repeat the agent's previous action verbatim (perseveration);
3. with p=0.3 keep the action but corrupt `talk`/`remember` text by token-level repetition and shuffling (`coherence.degrade(text, severity)`).

Every corruption is logged as `event(kind="degeneration", payload={mode, T_eff, T_c, tier})`. The dashboard's "degeneration events per generation" and "entropy histogram" read from these rows and from `llm_calls.effective_temperature`.

`coherence.score(text)` = geometric mean of (distinct-2-gram ratio, 1 - longest-run fraction, length sanity) ∈ [0,1], recorded for every decision, so degeneration is also *measured*, not only induced. On providers with real temperature the measured score is the primary signal; on Claude 5.x it is a control that the operator does what it claims.

---

## 9. Economy (`void/economy/*`)

### 9.1 Wallet
- `gate(agent) -> bool`: `balance >= min_call_reserve` **and** daily cap has headroom for `min_call_reserve` **and** total cap has headroom. If the gate fails because of the agent's own balance, the agent is declared bankrupt this tick. If it fails because of the daily cap, the agent is put to sleep for the day (fcfs policy) or all agents are (sync_sleep policy, triggered when `spend_today + n_awake * min_call_reserve > daily_cap`).
- `meter(agent, usage, tier) -> cost`: cost from `pricing.cost(usage, tier)`; debit; if balance would go below zero, clamp to zero and mark bankrupt (the call already happened; the metered overrun is recorded as `overrun` in the ledger payload). Increment `spend_today` and `spend_total`.
- `transfer(a, b, amount)`, `credit(agent, amount, kind, ref)`, `debit(...)` are atomic in one SQLite transaction with the ledger row.
- `spawn_pool` is a kernel-owned balance funding replacements; when it is empty no replacement is spawned (population may shrink; that is a result, not a bug).

### 9.2 Pricing
`cost_micro = in*price_in + out*price_out + cache_read*price_cache_read + cache_write*price_cache_write` per million tokens, rounded up to the nearest micro-dollar.

### 9.3 Scheduler
Day = `ticks_per_day` ticks. All alive agents wake at tick 0 of a day (`asleep=0`). `sleep` sets `asleep=1` for the rest of the day; it is one-way. Day end restores entropy budgets, resets `spend_today`, resets per-agent weather nudge allowance, and triggers the chronicle.

### 9.4 Task board
Operator posts tasks (`POST /tasks`). Agents `apply_task` with a pitch; the fee is charged immediately (costly signal). Operator approves one application (`POST /tasks/{id}/approve/{application_id}`): task becomes `assigned`; when the assigned agent next performs `forage` at any node... no — completion is an operator action too (`POST /tasks/{id}/complete`), because v1 has a human judge by design. Reward is credited on completion. Pending applications to a task that gets assigned are rejected (fees are not refunded).

### 9.5 Benefactor
When enabled, a seeded Poisson process (`mean_interval_ticks`) picks a target per `targeting` and credits an amount from the range. The agent sees a `windfall` in `heard` as "You found N dollars. You do not know where it came from." The event row is `visibility=operator` with the true provenance; the chronicle never prints it. Analysis (`scripts/analyze.py --benefactor`) reports: notes mentioning the windfall, spread of such notes across agents via gossip provenance, transfers/gadgets/talk referencing it, and whether those persist after grants stop.

---

## 10. Memory (`void/memory/*`)

- Vault: `vaults/<agent_id>/self.md`, `vaults/<agent_id>/notes/<slug>.md`. Note format:

```markdown
---
id: 01J...
title: The eastern node is thin
created_tick: 137
importance: 0.7
tags: [resources]
provenance: {source_agent: null, hop: 0, origin_note: null, origin_generation: 0}
---
Foraged at [[Node east]] and got little. [[Bao]] said the north node is better.
```

- Embedder: feature hashing of lowercased word unigrams + bigrams into 256 dims, signed hashing, L2-normalised. Deterministic, dependency-free, `Embedder` protocol allows swapping in a real model later.
- Retrieval (`mode: graph`): cosine top-`entry_k` notes as entry points, BFS up to `hops` following `[[wikilinks]]` (title match, case-insensitive) with score `sim * hop_decay^hop`, then rank by `score * (0.5 + importance) * recency_weight` and return `max_retrieved`. `mode: vector` skips the walk (research question 3). Retrieval happens in the perceive step; there is no explicit recall action in v1 because retrieval is free and automatic.
- `remember` writes the note, attaches links (existing titles are linked; unknown titles are still written as links so later notes can resolve them, mirroring Obsidian).
- `self` node: `revise_self(text)` truncates to `self_max_words`, bumps version, writes `self.md` and a `self_versions` row. The current summary is always injected into the observation.
- Importance defaults 0.5; the scripted brain raises it for money events; the LLM may set tags but not importance (kept out of the schema to avoid gaming). Gossip-received notes carry `hop >= 1`.

---

## 11. Culture (`void/culture/*`)

### 11.1 Gossip
On `talk(target, text)` where both agents are within `talk_radius`: the listener gets a `HeardMessage`. Additionally, with `share_probability_on_talk` (or deterministically on `share_note`), one of the speaker's notes (highest importance among non-self notes, or the named one) is copied into the listener's vault:
- body paraphrased: `scripted` mode perturbs `mutation_rate` of content words (synonym table + drop + swap) with a per-hop seeded RNG; `llm` mode asks the listener's tier model to restate it in one sentence (metered to the listener).
- provenance: `source_agent=speaker`, `hop=speaker_note.hop+1`, `origin_note=speaker_note.origin or speaker_note.id`, `origin_generation`.
- event `gossip_transfer` with both ids and a similarity score between origin and copy (embedding cosine), so drift per hop is a measured quantity.

### 11.2 Chronicle
At day end, `chronicle/day_<n>.md` is written from public events: births, deaths, transfers, big forages, weather, epochs, gadget verifications, task assignments, and the day's degeneration count. `template` writer is deterministic; `llm` writer is metered to a kernel wallet (`chronicle_budget_usd`) and falls back to template if that is empty. The headline is injected into every observation next day; the full text is what `read_chronicle` returns (as `last_action_result`).

### 11.3 Graveyard
On bankruptcy: vault directory moved to `graveyard/<agent_id>/`, notes marked archived (still queryable for lineage), status `archived` after the replacement is spawned. `void inspect --tree` prints the family tree from `parent_id`.

---

## 12. Sandbox and gadgets (`void/sandbox/*`)

A gadget is a Python module:

```python
# gadget.py (agent-authored)
def describe() -> dict:            # declarative render + effect spec, validated by the kernel
    return {"render": {"shape": "pyramid", "color": "#ffcc00", "scale": 1.2, "label": "beacon"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}
def run(params: dict) -> dict:     # pure function of params; result is validated against effect whitelist
    return {"value": min(0.2, params.get("x", 0) * 0.1)}
```

Test source (`tests.py`, agent-authored) is plain Python with `assert`s; it imports `gadget`.

Gate pipeline (`gate.propose(agent, name, purpose, code, tests)`):
1. size check (`max_code_bytes`), fee debited (`gadget_fee_usd`) — a costly signal against spam proposals;
2. `static_check`: AST walk rejects `import`/`from` of anything outside `{math, random, statistics, itertools, functools, json, re, collections, dataclasses, typing}`, any attribute access on `__builtins__`, names `eval exec compile open __import__ globals locals getattr setattr delattr breakpoint input`, and any dunder attribute access;
3. `SubprocessSandbox.run(code, tests)`: writes both files to a fresh temp dir, runs `python -I -S -B runner_stub.py` under `unshare -n` when available (`network_isolated` flag recorded either way), with `resource.setrlimit` for CPU, address space, file size (1 MB), open files (16), and no environment; wall-clock timeout kills the process group; stdout/stderr capped at 8 KB;
4. `describe()` output validated: `render.shape ∈ {cube, sphere, pyramid, cylinder, torus}`, colour is a hex string, scale ∈ [0.3, 3], label ≤ 24 chars; `effect.kind ∈ {forage_bonus, weather_shield, talk_range}` with value clamped by the kernel's per-effect cap; anything else -> rejected;
5. `gate_enabled: false` skips step 3 (research question 4) but never skips 2 or 4;
6. verified gadgets get a position (the proposer's) and appear in snapshots as their render spec. `use_gadget` re-runs `run(params)` in the sandbox and applies the *clamped* effect for the next `effect_ticks` ticks.

No gadget code ever reaches the browser. The renderer only sees `render_spec`.

`DisabledSandbox` rejects every proposal with reason `sandbox_disabled`, so a misconfigured deployment fails closed.

---

## 13. Reproduction and selection (`void/agents/lifecycle.py`)

`create_offspring(name, endowment)` is a single transaction:
1. preconditions: population < cap, `balance - endowment >= min_reserve`, endowment ≥ `min_call_reserve * 4`;
2. child row: `parent_id`, `generation = parent.generation + 1`, tier = parent tier, mutated with probability `population.tier_mutation_prob` (default 0.1) to a uniformly random other configured tier; `personality_seed = mutate(parent.seed, rng)` (each trait ± N(0, 0.1) clipped, motto re-derived from the parent's current `self` summary); position beside the parent; `entropy_budget` full;
3. vault: `self.md` = "Child of <parent>. <parent's self summary, first sentence>."; top-`inherit_top_k` parent notes by importance copied with provenance `hop=0, origin_generation=parent.generation` and tag `inherited`;
4. ledger: `endowment_out` on the parent, `endowment_in` on the child;
5. events `birth`.

Bankruptcy replacement: when an agent goes bankrupt and `population < cap` and `spawn_pool >= replacement_grant`, pick a parent among alive agents with probability ∝ balance (fitness = balance; the fitness function is kernel-owned and agents cannot touch it), and run the same offspring transaction with the grant coming from the spawn pool (`spawn_grant`). If no alive agent exists, spawn a fresh agent from the initial roster template.

Population cap and spend caps are enforced independently. Hitting either is an event, never an exception.

---

## 14. Tick loop (`void/sim/loop.py`)

```
async def tick():
    clock.advance()                                   # tick, day, tick_of_day
    if clock.new_day: scheduler.begin_day()           # wake all, restore entropy, reset spend_today, chronicle(prev day)
    epochs.apply_if_due(day)                          # sets scarcity / weather baseline
    benefactor.maybe_grant(tick)
    weather.step()                                    # decay toward baseline
    resources.step()                                  # regen
    awake = [a for a in agents.alive() if not a.asleep]
    if scheduler.policy == sync_sleep and not wallet.daily_headroom(len(awake)): scheduler.sleep_all(); awake = []
    order = rng("order", tick).shuffle(sorted(awake))  # deterministic per tick, not alphabetical every tick
    obs = {a: observation.build(a) for a in awake}     # includes memory retrieval
    sampling = {a: entropy.step(a, obs[a]) for a in awake}
    gated = [a for a in order if wallet.gate(a)]       # may bankrupt or sleep agents
    results = await gather(brain(a).decide(obs[a], sampling[a]) for a in gated)   # bounded concurrency
    for a in order that were gated:
        wallet.meter(a, results[a].usage)             # may bankrupt after the fact
        decision = results[a].decision or idle
        if sampling[a].degenerate: decision = entropy.corrupt(decision, rng("entropy", a, tick))
        coherence = coherence.score(results[a].raw_text)
        memory.apply_ops(a, decision.memory_ops)
        outcome = kernel.apply(a, decision.action, obs[a])   # validates; idle on failure; emits events
        record llm_calls row
    lifecycle.resolve()                               # bankruptcies -> graveyard + replacements; births
    culture.gossip.flush()                            # transfers queued by kernel talk outcomes
    metrics.record(tick); snapshot.publish(tick)      # EventBus -> WebSocket
```

Determinism rule: every random draw goes through `rng(stream, *keys)` = `random.Random(hash(seed, stream, keys))`. No global `random`, no `time`-dependent branches in the kernel. Brain latency is measured but never influences state.

---

## 15. WebSocket protocol (`void/server/ws.py`)

Client connects to `/ws`. Server sends:

```jsonc
{"type":"hello","run_id":"...","config":{...public subset...},"tick":0}
{"type":"snapshot","tick":N,"day":D,"tick_of_day":t,"weather":0.1,"scarcity":1.0,
 "spend_today_usd":0.4,"spend_total_usd":3.1,"spawn_pool_usd":4.2,
 "agents":[{"id","name","tier","color","x","y","heading","balance_usd","stress","asleep","status","generation","parent_id"}],
 "nodes":[{"id","x","y","stock","capacity"}],
 "gadgets":[{"id","name","owner","x","y","render":{...}}],
 "tasks":[...], "chronicle_headline":"..."}
{"type":"event","seq":123,"tick":N,"kind":"talk|move|forage|transfer|birth|death|degeneration|gossip_transfer|gadget_verified|windfall|epoch|...","agent_id":"...","payload":{...}}
```

A full `snapshot` is sent every tick (world is small); events are sent as they are emitted. The client keeps a ring buffer of snapshots keyed by tick and renders at `now - render_delay`, interpolating positions between the bracketing snapshots (the AI Town history-buffer idea). When `tick_seconds` is 0 the client renders as fast as snapshots arrive.

Operator HTTP:

| Method | Path | Body |
|---|---|---|
| GET | `/api/state` | latest snapshot |
| GET | `/api/agents/{id}` | record + self history + notes list |
| GET | `/api/agents/{id}/notes/{note_id}` | markdown |
| GET | `/api/chronicle?day=` | markdown |
| GET | `/api/metrics?since_tick=` | rows from metrics.jsonl |
| GET | `/api/tree` | family tree |
| POST | `/api/tasks` | `{title, description, reward_usd}` |
| POST | `/api/tasks/{id}/approve/{application_id}` | — |
| POST | `/api/tasks/{id}/complete` | — |
| POST | `/api/control/epoch` | `{kind, scarcity?, weather_baseline?, duration_days}` |
| POST | `/api/control/benefactor` | `{agent_id?, amount_usd}` (manual grant) |
| POST | `/api/control/pause`, `/resume`, `/step` | — |

---

## 16. Frontend (`web/`)

- `src/net/ws.ts`: connection, reconnect, message types mirrored from §15.
- `src/state/store.ts` (zustand): snapshot ring buffer (≤ 600), events feed (≤ 500), selected agent.
- `src/render/interp.ts`: `sample(buffer, renderTick) -> per-agent {x, y, heading, stress}` with linear interpolation and heading slerp; unit-tested with vitest.
- `src/scene/*`: ground grid sized from config, `Agent` = capsule, colour by tier, emissive pulse amplitude = stress, sleeping agents dimmed, bankrupt agents fade out over 1 s; `Node` = cone whose height tracks stock; `Gadget` = shape from render spec; weather drives fog density and sky tint; camera orbit controls from drei.
- `src/control/*`: right-side control room: wealth distribution (bar), entropy histogram (T_eff buckets), degeneration events per generation (bar), population and generation timeline (line), spend gauges, chronicle feed, events feed, task board with approve/complete buttons, epoch and benefactor forms, selected-agent panel (self summary history, notes list rendered as markdown).
- Charts are built per the `dataviz` skill; no chart library beyond lightweight SVG components.
- Build output is served by FastAPI at `/` when `web/dist` exists.

---

## 17. Research harness

- `void run --config configs/exp_tiers.yaml --seed 1 --days 10` writes `metrics.jsonl` (per tick: population, mean/median balance, gini, spend, degeneration count, mean stress, T_eff histogram, coherence mean; per day: births, deaths by tier, gossip transfers, mean drift per hop, gadget proposals/verified, chronicle length).
- `void compare runA runB` prints paired deltas with bootstrap CIs on the daily series.
- `configs/exp_*.yaml` encode the five research questions from the plan: tier robustness (`exp_tiers`), scarcity × tier (`exp_scarcity`), memory mode (`exp_memory_graph` vs `exp_memory_vector`), gate on/off (`exp_gate_on` vs `exp_gate_off`), vertical vs horizontal spread (`exp_spread`, with `gossip.enabled` toggled and reproduction endowment fixed).
- Every run stores its resolved config and hash; the analysis scripts refuse to compare runs whose configs differ outside the declared independent variable.

---

## 18. Safety guardrails (as implemented, not as aspiration)

1. No internet: the simulation never opens a socket except to the model provider. Gadgets run under `unshare -n`; when that is unavailable the gadget record says `network_isolated: false` and the operator sees it in the control room.
2. Declarative rendering: the browser receives only `render_spec` objects from a closed enum.
3. Kernel immutability: nothing in the agent action surface can change scarcity, caps, prices, thresholds, or the fitness function. `nudge_weather` is capped per agent per day and decays.
4. Typed, atomic reproduction and gadget calls; no agent-emitted SQL or code ever runs outside the sandbox.
5. Independent hard ceilings: population cap, daily spend cap, total spend cap, per-gadget CPU/memory/time.
6. Every LLM call is metered after the fact and gated before; the metered cost of the last call is what stops the next one.

---

## 19. Testing

- Unit tests per module (pricing math, wallet invariants under random ledgers, weather decay and caps, resource regen, retrieval ranking, wikilink parsing, static checker deny-list, sandbox timeout and memory kill, gossip provenance, offspring transaction rollback on failure, degeneration probability curve, coherence metric).
- `tests/test_e2e.py`: 6 scripted agents, 3 days, 24 ticks/day, asserting: no negative balances, `spend_total` equals ledger sum, population ≤ cap, every vault parses, snapshot schema validates, run is byte-identical on re-run with the same seed, and at least one degeneration event occurs under a stress-heavy config.
- `tests/test_anthropic_brain.py`: request construction and response parsing against a fake transport (no network), including refusal and malformed JSON paths.
- Frontend: `tsc --noEmit`, `vitest` for the interpolation buffer, `vite build`.

---

## 20. Known limits (stated so nobody oversells them)

- Claude 5.x models do not expose sampling temperature; on those tiers the entropy mechanism is a documented, uniformly applied operator plus prompt-visible stress, with coherence measured on real outputs. Providers that accept temperature get the real thing.
- The sandbox is process-level isolation, not a hypervisor. It is fail-closed and network-isolated, and the `SandboxRunner` protocol is where gVisor/Firecracker goes.
- The chronicle and gossip paraphrase default to deterministic template writers so runs are reproducible; the LLM writers are opt-in and metered.
- Nothing here resolves a philosophical question. `self_versions` is a browsable Ship of Theseus, not an answer to one.
