# The Void — Detailed Design (v2)

This document turns `docs/PLAN.md` into a buildable specification. Every interface, schema, formula and protocol that more than one module depends on is fixed here. Where the plan left a design decision open, this document makes the call and says why. Version 2 folds in a five-lens critique (safety, simulation correctness, research validity, renderer smoothness, plan coverage); the accepted changes are marked **[v2]** where they matter for an implementer.

The build is real: every mechanism below runs end to end with a deterministic brain and no API key, and the same code path runs against live Claude models when a key is present. The scripted brain is a genuine utility-sampling policy whose degeneration under temperature is real sampling degeneration, not a lookup table.

---

## 0. Decisions made where the plan left choices open

| Question | Decision | Why |
|---|---|---|
| Backend stack | Python 3.11, asyncio, FastAPI + uvicorn, SQLite (WAL), Pydantic v2, `anthropic` SDK 1.x | Author's daily stack; SQLite is inspectable; no external services to run |
| Reuse AI Town / Convex? | No. Same architecture (reactive store, history buffer, client interpolation) re-implemented self-contained | Convex needs a hosted deployment; the plan wants a closed world that runs anywhere |
| Frontend | Vite + React + TypeScript + react-three-fiber + drei + zustand; instanced meshes; a history buffer outside React | Smoothness is a stated requirement; React reconciliation must never be in the per-frame path |
| Economy income source | Resource nodes agents forage from, plus task rewards, transfers, benefactor windfalls | Without a baseline income every run is a bankruptcy race |
| Blank world | Resource nodes are the only pre-placed objects; nothing else is authored | Foraging needs somewhere to forage; everything else is built by agents |
| Currency | Real US dollars stored as integer micro-dollars; debits round up, credits round down | The fiction and the real safety cutoff must be one unit |
| World price vs real price **[v2]** | `economy.world_pricing: real | equalized`. Real cost is always metered and capped; in `equalized` mode every tier is debited at one price so tier is not confounded with economic stress | Research Q1/Q2 need tier decoupled from burn rate |
| Daily cap policy | `fcfs` (default) or `sync_sleep`, both implemented | The plan flagged it as deliberate |
| Sampling temperature on Claude 5.x | Not exposed by the API and not exposed by SDK 1.x for any model; on tiers that accept it (Haiku 4.5) it is sent via `extra_body`. Entropy drives prompt-visible stress; the kernel degeneration operator is optional (`induce`/`observe`/`both`) | Honest about the API; experiments run in `observe` mode so results are measured, not configured |
| One LLM call per tick | One structured-output call returning `Decision` | Cost predictability; the gate needs a bounded worst case |
| Sandbox isolation **[v2]** | `unshare -Urmpf --kill-child --mount-proc` (user, mount, pid namespaces) with tmpfs over the data dir, repo, HOME and /tmp, a bind-mounted work dir, read-only root, no network, rlimits, allow-list static checker, curated builtins. Fails closed if the self-test fails | Docker/gVisor unavailable in the build box; namespaces are available unprivileged |
| Replacement on bankruptcy | Fitness-proportional parent among survivors, mutated, funded by a kernel spawn pool; the dead agent's estate returns to the pool | Bankruptcy becomes a selection signal and money is conserved |
| Inheritance | Personality seed (mutated), tier (mutated with small probability), top-K notes, endowment, remaining weather allowance | Money-only inheritance is franchising, not evolution |
| Operator writes **[v2]** | HTTP handlers enqueue commands; the tick loop applies them at the next tick boundary and logs them | Single writer; replayable runs |
| Character art | Primitives in v1; the snapshot carries an `anim` state per agent as the hook for rigged clips | Prove the loop before the art pass |
| Donations / external task boards | Out of scope (PLAN §11) | Restated so it is not silently dropped |

---

## 1. Runtime topology

```
   config.yaml + seed
          │
   ┌──────▼──────────────────────────────────────────────────────┐
   │ void.sim.loop.Simulation   (one asyncio task, single writer) │
   │ tick():                                                       │
   │  1 clock.advance; begin_day; drain operator commands          │
   │  2 epochs, benefactor, weather, resources, effects expiry     │
   │  3 observations (memory retrieval)  → entropy.step            │
   │  4 gate with reservation, decide concurrently, meter per call │
   │  5 TICK TRANSACTION: corrupt?, memory ops, kernel.apply,      │
   │    gossip.flush, lifecycle.resolve, metrics, snapshot, tick   │
   │  6 end-of-run checks (capped_total, extinct)                  │
   └───────┬──────────────────────────────────────┬───────────────┘
           │ SQLite world.db, vaults/*.md          │ EventBus
           │                                       ▼
           │                        FastAPI: /ws (outbox per client), /api/*
           │                                       ▼
           │                        web/: History (typed arrays) → R3F scene
           │                                 zustand → control room
```

The referee/kernel is `void.world.kernel.Kernel`. It is the only writer to world state. Brains return a `Decision`; the kernel decides what happens.

---

## 2. Repository layout

```
pyproject.toml            uv-managed; package `void`
void/
  config.py rng.py ids.py db.py events.py types.py
  world/      state.py kernel.py weather.py resources.py epochs.py
  agents/     models.py registry.py lifecycle.py
  brain/      decision.py base.py prompt.py scripted.py anthropic_brain.py entropy.py
              coherence.py claims.py gadget_templates.py factory.py
  economy/    pricing.py wallet.py scheduler.py taskboard.py benefactor.py
  memory/     embed.py notes.py vault.py graph.py store.py
  culture/    gossip.py chronicle.py
  sandbox/    static_check.py runner.py runner_stub.py gate.py registry.py
  sim/        observation.py loop.py snapshot.py metrics.py control.py
  server/     app.py ws.py api.py auth.py
  cli.py      run | serve | analyze | compare | inspect | schema | mock-feed
configs/      base.yaml scripted_smoke.yaml live_two_tier.yaml exp_*.yaml
schemas/      hello/snapshot/event/metrics JSON Schema (exported, committed)
web/          Vite + R3F (§16)
tests/        one file per module + test_e2e.py + test_experiments.py
scripts/      analyze.py compare.py
docs/         DESIGN.md PLAN.md RUNBOOK.md
```

---

## 3. Data model (SQLite)

Money: `INTEGER` micro-dollars. Positions: `REAL`. JSON columns are `TEXT`.

```sql
run(run_id PK, config_hash, config_yaml, seed, created_at, schema_version,
    status CHECK IN ('running','completed','capped_total','extinct','inconsistent'),
    ended_tick, ended_reason, operator_token_hash, experiment, arm)
agents(agent_id PK, name, parent_id, model_tier, balance CHECK(balance>=0), personality_seed JSON,
       memory_path, generation, status CHECK IN ('alive','bankrupt','archived'),
       x, y, heading, entropy_budget, stress, asleep, last_forage_tick, last_action JSON,
       last_action_ok, weather_nudge_used, born_tick, died_tick, created_at)
wallet_ledger(entry_id PK, tick, agent_id, delta, balance_after, kind, ref, payload JSON)
   -- kind: llm_call | forage | transfer_in | transfer_out | task_reward | pitch_fee | windfall |
   --       endowment_out | endowment_in | spawn_grant | estate_out | estate_in | gadget_fee |
   --       gadget_use_fee | overrun | chronicle_call | gossip_call
   -- agent_id may be a kernel wallet: 'house', 'spawn_pool', 'chronicle'
llm_calls(call_id PK, tick, agent_id, purpose ('decide'|'gossip'|'chronicle'), tier, model (served),
          provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
          real_cost, world_cost, hold, estimated (1 if usage unknown), latency_ms, stop_reason,
          effective_temperature, api_temperature, stress, degenerate_induced, corruption_mode,
          text_coherence, invalid_action, stale_action, perseveration, action_entropy_w8,
          action_regret, p_chosen, claims_made, claims_false, thought, raw_text, request_id, error)
events(seq PK AUTOINCREMENT, tick, day, kind, agent_id, payload JSON, visibility)
notes(note_id PK, agent_id, path, title, created_tick, importance, tags JSON, embedding BLOB,
      channel ('observed'|'gossip'|'inherited'|'chronicle'|'tracer'), source_agent_id, hop,
      origin_note_id, origin_generation, transfer_tick, archived)
note_links(from_note_id, to_title)
self_versions(agent_id, version, tick, summary)
tasks(task_id PK, title, description, reward, posted_tick, status, assigned_agent_id, completed_tick)
task_applications(application_id PK, task_id, agent_id, pitch, fee, tick, status)
gadgets(gadget_id PK, name, owner_agent_id, purpose, code_path, test_path, code_hash, test_hash,
        status ('proposed'|'rejected'|'verified'), render_spec JSON, effect_spec JSON,
        verification JSON, verified_without_tests, template_label (operator-only), x, y,
        created_tick, uses, failures)
agent_effects(agent_id, kind, value, expires_tick, PK(agent_id, kind))
gadget_uses(gadget_id, agent_id, tick, ok, realized_effect, expected_effect)
resource_nodes(node_id PK, x, y, stock, capacity, regen_per_tick)
world_kv(key PK, value JSON)   -- tick, day, weather, weather_baseline, scarcity, epoch_stack,
                               -- spend_today, spend_total, spawn_pool, house, chronicle_wallet,
                               -- sandbox_runs_today, sandbox_cpu_today, benefactor_next_tick, ...
control_commands(cmd_id PK, received_tick, kind, payload JSON, applied_tick)
tracer_arrivals(agent_id, tick, channel, hop, PK(agent_id))
chronicle_items(day, item_id, kind, event_seq)
snapshots(tick PK, json)   -- ring of run.snapshot_ring
metrics(tick PK, day, json)
```

Vault markdown is the source of truth for memory content; `notes` is a rebuildable index.

---

## 4. Configuration

All keys below exist in `void/config.py` (pydantic, `extra=forbid`) and are covered by `VoidConfig.hash()`. Defaults shown are `configs/base.yaml`.

```yaml
run: {name, seed, days: 10, ticks_per_day: 24, tick_seconds: 0, data_dir, snapshot_ring: 600}
world:
  size: 60, max_speed: 2.0, talk_radius: 4.0, forage_radius: 1.5, gadget_radius: 2.0
  resource_nodes: 6, node_capacity: 40, node_regen_per_tick: 0.4
  forage_yield_usd: 0.02, forage_units_per_tick: 1.0, scarcity: 1.0
  weather: {baseline: 0, bounds: [-1, 1], nudge_cap_per_agent_day: 0.1,
            global_nudge_cap_per_tick: 0.2, decay_per_tick: 0.10, yield_sensitivity: 0.5,
            yield_mult_bounds: [0.2, 2.0]}
  effect_ticks: 12
  effect_caps: {forage_bonus: 0.25, weather_shield: 0.5, talk_range: 2.0}
population: {cap: 12, min_reserve_usd: 0.50, spawn_pool_usd: 5.0, replacement_grant_usd: 0.75,
             starting_balance_usd: 1.50, tier_mutation_prob: 0.10, min_endowment_calls: 4,
             reproduction_enabled: true}
economy:
  daily_cap_usd: 3.0, total_cap_usd: 30.0, daily_cap_policy: fcfs
  pitch_fee_usd: 0.02, min_call_reserve_usd: 0.05, hold_multiplier: 1.0
  world_pricing: real, equalized_price_in_per_mtok: 4.0, equalized_price_out_per_mtok: 20.0
  chronicle_budget_usd: 0.50
tiers.<name>:
  provider (anthropic|scripted), model, price_in/out/cache_read/cache_write_per_mtok
  supports_temperature, refusal_fallbacks, effort, architecture, collapse_temperature,
  max_temperature, entropy_budget_max, max_tokens, color, scripted_beta (hidden softmax
  sharpness for scripted tiers; the empirical collapse point the analysis must recover)
agents: [{name, tier, personality?}]
entropy:
  base_temperature: 0.7
  drain: {baseline 1.0, low_balance 6.0, failed_action 4.0, crowding 0.5, weather 2.0, hunger 3.0, hunger_ticks 6}
  restore_on_sleep: 1.0
  degeneration: {mode: both (induce|observe|both), p_at_collapse 0.15, p_at_max 0.90,
                 mode_weights {random_action .4, perseverate .3, garble .3}}
memory: {embedding_dim 256, entry_k 4, hops 2, hop_decay 0.6, max_retrieved 8, mode graph|vector,
         inherit_top_k 3, self_max_words 60, note_max_chars 600, recency_half_life_ticks 96,
         auto_link_k 2, auto_link_min_sim 0.35, probe_every_ticks 0}
gossip: {enabled, share_probability_on_talk 0.35, paraphrase scripted|llm, mutation_rate 0.15,
         max_llm_paraphrases_per_tick 4}
chronicle: {enabled, writer template|llm}
sandbox: {enabled, gate_enabled, runner subprocess|disabled, require_isolation true,
          cpu_seconds 2, memory_mb 256, timeout_seconds 5, max_code_bytes 8000,
          max_output_bytes 8192, gadget_fee_usd 0.05, use_fee_usd 0.01,
          max_gadgets_per_agent 3, max_gadgets_total 24, proposals_per_agent_per_day 1,
          max_runs_per_tick 4, max_runs_per_day 50, cpu_seconds_per_day 60,
          propose_window_ticks 12}
benefactor: {enabled false, disclosure opaque|legible, mean_interval_ticks 40,
             amount_usd [0.25, 1.0], targeting random|poorest|richest, start_day 1, stop_day null}
epochs: [{day, kind drought|storm|boom|arrival|custom, scarcity?, weather_baseline?,
          duration_days 1, arrival?: {name, tier, balance_usd}}]
experiment: {name null, arm null, independent_variables [], primary_outcome null,
             secondary_outcomes [], tracer: {enabled false, day 1, agent, title, body,
             importance 0.95, node_index 0}}
server: {host, port, max_concurrent_calls 4, render_delay_ticks 2}
```

Load-time validation prints warnings (never fails) when: `estimated_call_usd(tier) / forage_income_per_tick` is outside `[0.3, 2.0]` for any tier used by the roster; `total_cap_usd < days * daily_cap_usd`. It fails when `total_cap_usd < daily_cap_usd`, when an agent references an unknown tier, or when a tier's `collapse_temperature > max_temperature`.

`VoidConfig.hash()` is sha256 over canonical JSON. Two runs with equal hash and seed under scripted brains produce byte-identical `metrics.jsonl` (minus the excluded wall-clock fields `created_at`, `latency_ms`, `ts_ms`).

---

## 5. Core types (`void/types.py`)

As in v1 plus **[v2]** `ActionOutcome.kind ∈ {ok, invalid, stale}` (`stale` = valid against the observation but not against live state; it does not add `failed_action` drain), `Observation.heard` capped at 8 messages / 1500 chars newest first, `Observation.anim_hint`. Every list that feeds a decision is sorted: agents by `agent_id`, neighbours and nodes by `(distance, id)`.

---

## 6. Decision schema (`void/brain/decision.py`)

```python
class Claim(BaseModel):            # [v2] factual claims the speaker makes in `text`
    subject: Literal["node", "agent", "self", "weather"]
    id: str | None                 # node_id or agent_id
    attr: Literal["stock_bucket", "balance_bucket", "weather_label", "stress_bucket"]
    value: str

class MemoryOp(BaseModel):  op: remember|revise_self; text ≤ 600; title ≤ 60; links_to ≤ 5; tags ≤ 5
class Action(BaseModel):
    type: ActionType
    target ≤ 80; x, y ∈ [-1e4, 1e4] finite; text ≤ 280; note_title ≤ 60
    amount_usd ∈ [1e-6, 1000] finite; delta ∈ [-1, 1] finite
    name: ^[A-Za-z][A-Za-z0-9 _'-]{0,19}$ ; code, tests ≤ 8000 chars
    params: ≤ 8 keys matching ^[a-z_]{1,16}$, finite values in [-1e6, 1e6]
    claims: list[Claim] ≤ 4 (talk / apply_task only)
class Decision(BaseModel): thought ≤ 600; memory_ops ≤ 2; action
```

`strict_json_schema()` strips validation keywords and marks every object strict (all properties required, optionals nullable). Semantic validation is the kernel's (§7.3). The kernel strips control characters and replaces `<`/`>` with full-width equivalents in every free-text field before storage.

---

## 7. Brains

### 7.1 Protocol
`Brain.decide(obs, sampling) -> BrainResult(decision|None, usage, latency_ms, stop_reason, raw_text, request_id, error, model)`. Errors never raise out of `decide`. `error` prefixes: `refusal:`, `parse:`, `api_<status>:`, `rate_limit:`, `connection:`, `timeout:`. **[v2]** For `timeout:` and `connection:` the loop meters the *hold* with `estimated=1` (the provider may have billed); for `api_*` it meters nothing.

### 7.2 ScriptedBrain **[v2 argument generators]**
Utility per available action (personality-weighted), softmax at `T_eff / scripted_beta`, drawn from `rng("brain", agent_id, tick)`. Argument generators, one per action type:

| action | arguments |
|---|---|
| move | toward the best-known node (rich > thin) with jitter; random wander when no node is known; toward the tracer node when a `strategy` note is in memory (adoption is behavioural) |
| forage | none |
| talk | templated sentence(s) about the nearest node, weather, own purse, a remembered note, the motto; **claims** attached for every factual sentence; a claim is deliberately false with probability `(1 - honesty) * stress` (documented as the scripted stand-in for deception) |
| share_note | highest-importance non-self note title |
| transfer | to the most stressed neighbour, amount = greed-scaled fraction of surplus above `min_reserve` |
| read_chronicle | once per day with p = curiosity when a headline exists |
| apply_task | templated pitch from motto + task title |
| nudge_weather | sign toward yield-positive, magnitude = remaining allowance |
| propose_gadget | a template from `gadget_templates.py` (2 valid, 1 fails static check, 1 fails tests, 1 fails spec validation, 1 oversized, 1 valid-but-broken-run) chosen with `gadget_defect_rate` (config `tiers.<scripted>.gadget_defect_rate`, default 0.3) |
| use_gadget | nearest gadget, `params={"x": rng}` |
| create_offspring | when balance ≥ 2×min_reserve + endowment; endowment = `min_call_reserve × min_endowment_calls`; name = parent name + generation |
| sleep | when burn rate exceeds expected forage yield, or late in the day with caution |
| idle | fallback |

Memory ops: `remember` on windfalls, transfers, gossip, notable forages (always with `links_to` = node/agent entity titles); `revise_self` once per day. `action_regret = U_max − U_chosen` and `p_chosen` are returned in `BrainResult` extras for the scripted tier only.

Synthetic usage: `input = tokens(system) + tokens(rendered obs)`, system portion billed as `cache_read` after the agent's first call; `output = tokens(decision json)`. The scripted tier is priced like the frontier tier so the economy is exercised.

### 7.3 AnthropicBrain
As built: `client.messages.create` (or `client.beta.messages.create` with `betas=["server-side-fallback-2026-07-01"], fallbacks="default"` when `refusal_fallbacks`), `output_config={"format": {"type": "json_schema", "schema": ...}, "effort": tier.effort}`, cache-controlled system block, `extra_body={"temperature": api_t}` only when `supports_temperature`. `api_temperature = min(1, T_eff / max_temperature)`. Stop reasons handled: `refusal` → `decision=None`; `max_tokens` → parse attempt; parse failures recorded. Served `response.model` and `request_id` stored per call.

### 7.4 Kernel validation **[v2]**
`Kernel.validate(agent, action)` reads **live** state only. Preconditions that earlier agents in the same tick can invalidate (population cap, node stock, task status, target alive+awake+in range, balance) are re-checked in `apply`. Outcome kinds: `ok`, `invalid` (never valid; adds `failed_action` drain), `stale` (valid against the observation; no drain). Invalid or stale actions are applied as `idle` with a reason in `last_action_result`.

---

## 8. Entropy model

Per agent per tick:

```
drain  = baseline + low_balance·max(0, 1 − balance/starting) + failed_action·[last invalid]
       + crowding·n_neighbours + weather·|weather|·(1 − weather_shield) + hunger·[ticks_since_forage > hunger_ticks]
budget = max(0, budget − drain);  stress = 1 − budget/budget_max
T_eff  = base_T + (max_T − base_T)·stress
p_deg  = 0 if T_eff < T_c else lerp(p_at_collapse, p_at_max, (T_eff − T_c)/(max_T − T_c))
degenerate_induced = mode ∈ {induce, both} and rng("degenerate", agent, tick) < p_deg
```

`ticks_since_forage` for a newborn counts from `born_tick`. Budget is restored by `restore_on_sleep·budget_max` at sleep or day end.

**Operator** (`entropy.corrupt`, drawn from `rng("corrupt", agent, tick)`): `random_action` draws only from `{move, forage, talk, idle, sleep, read_chronicle, nudge_weather}` with random valid arguments (never money-moving or sandbox actions); `perseverate` repeats the agent's stored `last_action` (a previous transfer may repeat: that is the intended cost of degeneration); `garble` applies `coherence.degrade` to `thought`, `action.text` and memory texts with severity from `(T_eff − T_c)/(max_T − T_c)`. Every corruption is an event `degeneration {mode, t_eff, t_c, tier, generation, agent_name}`.

**Measured degradation** **[v2]** stored on every `llm_calls` row, computed *after* any corruption:
- `text_coherence`: `coherence.score` over the free-text fields only (thought, action.text, memory texts), null when under 12 tokens;
- `invalid_action` (0/1, kernel `invalid` or parse/refusal), `stale_action` (0/1);
- `perseveration` (0/1: same type+target as the previous tick);
- `action_entropy_w8`: Shannon entropy of action types over the agent's last 8 ticks;
- `action_regret`, `p_chosen` (scripted only);
- `claims_made`, `claims_false` (§9.6).

In `observe` mode the operator never runs; stress remains prompt-visible and real temperature is still sent where supported. Every `exp_*` config uses `observe`. With `induce`/`both` the "degeneration per tier" chart is a calibration check, not a finding, and the dashboard labels it so.

---

## 9. Economy

### 9.1 Wallet **[v2 reservation model]**
- `hold(tier, prompt_tokens) = ceil(hold_multiplier · (prompt_tokens·price_in + max_tokens·price_out + system_tokens·price_cache_write))`, floored at `min_call_reserve_usd`. `prompt_tokens` is estimated from the actual rendered observation (`len/4 · 1.3`).
- `gate(agent, tier, hold)`: passes only if `balance ≥ hold`, `spend_total + reserved_total + hold ≤ total_cap`, `spend_today + reserved_today + hold ≤ daily_cap`; on pass the hold is added to the in-memory reserved counters. Reasons: `ok | insufficient | daily_cap | total_cap`.
- `meter(agent, tier, usage, hold, purpose, ...)`: releases the hold, computes `real_cost` (from usage) and `world_cost` (real, or equalized), debits `world_cost` from the agent (rounded up), adds `real_cost` to `spend_today`/`spend_total`. If `world_cost > balance` (only possible on a provider quirk since the hold bounded it) the difference is debited from the `house` wallet with ledger kind `overrun`, so every dollar has a row and the CHECK constraint holds. The metering transaction commits immediately (it is the real-money record), independent of the tick transaction.
- `charged_call(payer, tier, hold, purpose, coro)` is the single choke point for any LLM call (decide, gossip paraphrase, chronicle). A test greps that `messages.create` appears only in `anthropic_brain.py`.
- Neither `gate` nor `meter` changes agent status. Bankruptcy is decided once, at `lifecycle.resolve` (§13).
- Kernel wallets: `spawn_pool`, `house`, `chronicle` (seeded from `chronicle_budget_usd`; its real spend counts toward the caps).
- `transfer`: target alive and awake, not self, `balance_after ≥ min_reserve` (the reserve applies to every voluntary outflow: transfers, endowments, fees).

### 9.2 Pricing
`real_cost = ceil((in·p_in + out·p_out + cr·p_cr + cw·p_cw)/1e6 · MICRO)`; `world_cost` the same with equalized prices when configured. Both are stored on the ledger row and the call row.

### 9.3 Scheduler
Unchanged. A second `sleep` in a day is `invalid`. Under `sync_sleep`, when `spend_today + n_awake·hold_min > daily_cap` all awake agents sleep. Under `fcfs` the gate decides one agent at a time in tick order.

### 9.4 Task board
Operator posts (`POST /api/tasks`), agents apply (fee charged immediately; pitch length ≤ 280), operator approves one application (others rejected, fees kept), operator completes (reward credited). All operator writes go through the command queue (§14.1).

### 9.5 Benefactor **[v2]**
`disclosure: opaque | legible`. Both arms credit identical seeded amounts to identical targets at identical ticks (shared stream `benefactor`, keyed by tick only). `legible` delivers a `task_reward` with reason "Reward for services rendered."; `opaque` delivers the neutral string "Your balance increased by $N. No source is recorded." (exact string stored in the run table). Any note the recipient writes in the same or next tick is tagged `windfall_seeded` kernel-side. Grants happen only in `[start_day, stop_day]`. Analysis (§17) reports lineage penetration, exogenous vocabulary, costly actions after grants relative to before and to the legible arm, and persistence after `stop_day`. The scripted arm is a pipeline test only.

### 9.6 Claims and deception **[v2]**
For `talk` and `apply_task`, each `Claim` is checked against live state at that tick (`node.stock_bucket`, `agent.balance_bucket` ∈ {empty, thin, comfortable, rich}, `weather_label`, `stress_bucket`) and an event `claim {claim, truthful}` is emitted. `claims_made`/`claims_false` go on the call row and `false_claim_rate` per tier/day into metrics. `PersonalitySeed` gains `honesty`.

---

## 10. Memory

As v1, plus **[v2]**:
- Provenance carries `channel ∈ {observed, gossip, inherited, chronicle, tracer}`, `transfer_tick`, `path: [agent_ids]`. Inherited copies get `channel=inherited, hop=parent_hop`; gossip copies `channel=gossip, hop+1`; a `remember` in the same or next tick after a successful `read_chronicle` gets `channel=chronicle, origin_note_id="chronicle:day_<n>"`.
- Auto-linking (A-MEM): on `remember`, the top `auto_link_k` existing notes above `auto_link_min_sim` are linked bidirectionally (`## Related` sections, both directions in `note_links`). Entity stub notes (`Node <id>`, `<Agent name>`) are created on first mention so links resolve.
- Note files are `notes/<slug>-<id6>.md`; slugs are alphanumeric only; titles are unique per vault (kernel appends `-2`).
- Per call: `retrieved_hop_hist`, `retrieved_mean_age`, `retrieved_channel_mix` in the call row's JSON extras; per day `notes_by_channel`, `link_density`. Optional `memory.probe_every_ticks` plants a note two hops from an entry point and records `probe_recall@k`.

---

## 11. Culture

### 11.1 Gossip
As v1 with provenance per §10, a per-hop `similarity` to the origin, and **[v2]** `paraphrase: llm` routed through `Wallet.charged_call(listener, ...)` with fallback to scripted paraphrase (`gossip_paraphrase_downgraded` event) and at most `max_llm_paraphrases_per_tick`.

### 11.2 Chronicle
Template writer prints sanitized names and numbers only, never agent prose. Each line gets an item id (`chronicle_items`). LLM writer pays from the `chronicle` wallet via `charged_call` and falls back to the template. Runs at day end for the day that ended; the headline is in every observation next day.

### 11.3 Graveyard
On bankruptcy: estate to spawn pool, vault moved to `graveyard/<agent_id>/`, `notes.path` and `agents.memory_path` rewritten in the same transaction, status `archived`. `GET /api/graveyard`, `/api/tree`, and `/api/agents/{id}` work for archived agents (self-version history included).

### 11.4 Epochs **[v2]**
Kinds `drought | storm | boom | arrival | custom`. Applying pushes the previous `scarcity`/`weather_baseline` on `world_kv.epoch_stack`; expiry at `day + duration_days` pops it. `arrival` spawns a generation-0 agent (population cap permitting). Scheduled and active epochs are in the snapshot.

### 11.5 Tracer **[v2]**
`experiment.tracer` writes one seeded note (channel `tracer`, tag `strategy`, body "Prefer node <id>; it is the richest.") into one agent's vault on a given day; the world places that node at 1.5× capacity so the claim is true. `tracer_arrivals` records the first arrival channel per agent. The scripted brain adopts the strategy behaviourally (§7.2).

---

## 12. Sandbox and gadgets **[v2]**

Gadget module contract: `describe() -> {render: {shape, color, scale, label, rotation_deg?, height_offset?}, effect: {kind, value}}`, `run(params) -> {value}`. Tests are assert-based and `import gadget`.

Gate pipeline (`gate.propose`):
1. availability (Voyager trigger): `propose_gadget` is available only if the agent had ≥1 invalid action in the last `propose_window_ticks` or holds no verified gadget; also requires `proposals_per_agent_per_day`, `max_gadgets_per_agent`, `max_gadgets_total`, and the daily run/CPU budgets;
2. fee debited; size check;
3. **static check (allow-list)**, applied identically to code and tests: permitted node types only; imports only from `{math, random, statistics, itertools, functools, json, re, collections, dataclasses, typing}`; `Attribute` allowed only when the attribute does not start with `_` and (the value is a local name or `(module, attr)` is in an explicit table); no `Subscript` on module objects; `Name` and `Attribute.attr` checked against a denied set (`eval exec compile open __import__ globals locals vars dir getattr setattr delattr type super memoryview breakpoint input help exit quit`); no `Global`/`Nonlocal`; no dunder attributes. The static check is an anti-footgun filter; the namespace sandbox is the security boundary;
4. **runner**: `unshare -Urmpf --kill-child --mount-proc sh launcher.sh <work> <data_dir> <repo> python3 -S -B -s -P /work/runner_stub.py <seed> <mode>`; the launcher bind-mounts the work dir at `/work`, overmounts tmpfs on the data dir, repo root, HOME and /tmp, remounts `/` read-only, and execs Python with `env={PYTHONHASHSEED: "0", PYTHONDONTWRITEBYTECODE: "1", PATH}`; `preexec_fn` sets RLIMIT_CPU/AS/FSIZE(1 MB)/NOFILE(16)/NPROC(8)/CORE(0); run via `asyncio.create_subprocess_exec` under `asyncio.wait_for(timeout_seconds)` and a global concurrency of 1; the stub seeds `random`, installs curated builtins (no `__import__`, `eval`, `exec`, `open`, `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `type`), redirects stdout/stderr to files, executes the gadget and tests, and writes `/work/result.json`; the kernel parses only `result.json` (≤ 8 KB, strict schema). At startup the runner self-tests; if the self-test fails and `require_isolation` is true, all gadget actions fail with `sandbox_unavailable`;
5. `describe()` validation: shape ∈ {cube, sphere, pyramid, cylinder, torus}; color `^#[0-9a-f]{6}$`; scale ∈ [0.3, 3]; label `^[A-Za-z0-9][A-Za-z0-9 _'-]{0,23}$`; effect kind ∈ caps table, value clamped to `[0, cap]`; effect kind and value frozen at verification;
6. `gate_enabled: false` skips step 4's test execution only (records `verified_without_tests=1`);
7. verified gadgets are placed on a 0.8-unit ring around the proposer (`rng("gadget_place", gadget_id)`), `code_hash`/`test_hash` stored; **every `use_gadget` re-hashes the stored code and refuses on mismatch** (`gadget_tampered`), runs `run(params)` in the sandbox, clamps the returned value to `[0, min(describe_value, cap)]`, and applies a non-stacking max-wins effect for `effect_ticks`; one use per gadget per agent per `effect_ticks`; `use_fee_usd` charged; distance ≤ `gadget_radius`; any agent may use any verified gadget; gadgets persist after the owner dies and are not inherited.

Effects: `forage_bonus` multiplies the user's own forage yield; `weather_shield` reduces the user's weather drain term; `talk_range` adds to the user's talk radius.

---

## 13. Reproduction and selection **[v2]**

Single end-of-tick evaluation in `lifecycle.resolve()`, in tick order:
1. births requested by `create_offspring` this tick were applied inside `kernel.apply` (population counter incremented immediately);
2. for each alive agent with `balance < hold_min(tier)` (or flagged by a metering overrun): `bankrupt(a)` → estate to `spawn_pool` (`estate_out`/`estate_in`), status `bankrupt`, `died_tick`, event `death {cause: gate|overrun}`; then `spawn_replacement` if `population < cap` and `spawn_pool ≥ replacement_grant`: parent chosen with weight `balance + 1000 µ$` among alive agents via `rng("replace", tick, agent)`, child funded by `spawn_grant` from the pool (no parent debit), event `birth {kind: replacement}`; refusals emit `replacement_skipped {reason: cap|pool_empty}`; if no alive agent exists the child is a fresh generation-0 agent from the roster template;
3. archive: vault → graveyard, paths rewritten, status `archived`.

`create_offspring` preconditions: `reproduction_enabled`, population < cap, `balance − endowment ≥ min_reserve`, `endowment ≥ min_call_reserve × min_endowment_calls`. Child: tier mutated with `tier_mutation_prob`, seed mutated (traits ± N(0, 0.1); motto from the parent's self summary), position beside parent, remaining weather allowance inherited, top-K notes copied with `channel=inherited`, `self.md` = "Child of <parent>. <first sentence of parent self>".

Run end states: after `resolve`, `end_run("capped_total")` when `spend_total + hold_min > total_cap`, `end_run("extinct")` when population is 0; `completed` after the last day. `end_run` writes the final chronicle, flushes metrics, emits `run_ended`; CLI exit code 0 for completed, 2 otherwise.

---

## 14. Tick loop

```
tick():
  new_day = clock.advance()
  if new_day: scheduler.begin_day(); wallet.reset_day(); sandbox.reset_day(); chronicle.write(prev_day)
  control.drain()                       # operator commands, cmd_id order, each logged (visibility=operator)
  epochs.apply_and_expire(day); benefactor.maybe_grant(tick); weather.step(); resources.step(); effects.expire(tick)
  if new_day and tracer due: memory.plant_tracer()
  order = rng("order", tick).shuffle(sorted(awake ids))
  obs[a] = observation.build(a)            # sorted lists, retrieval, heard cap
  budget[a], sampling[a] = entropy.step(a)  # persisted before deciding
  gated = []; for a in order: hold = wallet.hold(tier, obs_tokens); g = wallet.gate(a, tier, hold)
      if g.ok: gated.append(a) else: emit gate_denied; if daily_cap: sleep(a) (fcfs) ...
  if sync_sleep and not headroom: sleep_all
  results = await gather(brain(a).decide(obs[a], sampling[a]) for a in gated, return_exceptions=True)
  for a in gated (in order): wallet.meter(...)          # per-call transaction, commits immediately
  with db.tx():                                          # the tick transaction
     for a in order: decision = result or idle; corrupt if induced; measure; memory.apply_ops; kernel.apply
     gossip.flush(); lifecycle.resolve(); metrics.record(); snapshot.publish(); clock.save()
  end-of-run checks
```

### 14.1 Operator command queue **[v2]**
HTTP handlers only enqueue (`control_commands`, in-memory queue) and return `{cmd_id, will_apply_at_tick}`. `pause`/`resume`/`step`/`speed` toggle loop-level flags checked between ticks. `step` is valid only while paused and runs exactly one tick.

### 14.2 Determinism rules
All randomness via `RNG.stream(name, *keys)` (blake2b; distinct names `order`, `brain`, `degenerate`, `corrupt`, `benefactor`, `weather`, `replace`, `mutate`, `gadget`, `gadget_place`, `gossip`, `ids`). IDs are counter-based. Every query that feeds a decision has `ORDER BY`. `gather(..., return_exceptions=True)`; completion order never influences state. Wall-clock appears only in `created_at`, `latency_ms`, `ts_ms`. On startup, if `world_kv.tick < max(llm_calls.tick)` the run is marked `inconsistent` and refuses to resume.

---

## 15. WebSocket and HTTP protocol **[v2]**

Schemas are exported by `void schema` into `schemas/*.schema.json` and consumed by `web/` (`npm run gen:types`). `void mock-feed` writes a schema-valid fixture for renderer development.

Messages (server → client):

```jsonc
{"type":"hello","run_id","config":{world_size,ticks_per_day,tick_seconds,render_delay_ticks,population_cap,
   daily_cap_usd,total_cap_usd,tiers:{name:{color,model,provider}}},"tick","day","last_seq","paused",
   "server_ts_ms","status"}
{"type":"roster","agents":[{id,name,tier,generation,parent_id,born_tick,died_tick,status}]}   // on hello, birth, death
{"type":"snapshot","tick","day","tick_of_day","ts_ms","paused","weather","scarcity",
   "spend_today_usd","spend_total_usd","spawn_pool_usd","population","gadgets_rev","tasks_rev","chronicle_rev",
   "epochs":{"active":{...}|null,"scheduled":[...]},
   "agents":[{id,x,y,heading,stress,t_eff,degenerate,asleep,status,balance_usd,anim,last_action:{type,target,ok},effects:[kind]}],
   "nodes":[{id,x,y,stock,capacity,stock_delta}]}
{"type":"gadgets","rev","items":[{id,name,owner,x,y,render:{shape,color,scale,label,rotation_deg,height_offset},uses}]}
{"type":"tasks","rev","items":[{id,title,reward_usd,status,assigned_agent_id,posted_tick,
   applications:[{id,agent_id,pitch,fee_usd,status,tick}]}]}
{"type":"chronicle","rev","day","headline","markdown"}
{"type":"event","seq","tick","day","kind","agent_id","visibility","payload"}
{"type":"metrics","tick","day","row"}      // one metrics row per tick
{"type":"day","day","row"}                 // daily aggregate
{"type":"status","paused","tick_seconds","status"}
```

Event payloads fixed: `degeneration {mode,t_eff,t_c,tier,generation,agent_name}`, `gossip_transfer {speaker_id,listener_id,hop,origin_note_id,origin_agent_id,origin_generation,note_title,similarity}`, `death {agent_id,name,tier,generation,cause,replacement_id}`, `birth {agent_id,name,tier,generation,parent_id,kind,endowment_usd}`, `talk {speaker_id,listener_id,text}`, `transfer {from,to,amount_usd}`, `forage {agent_id,node_id,units,usd}`, `windfall {agent_id,amount_usd}`, `claim {agent_id,claim,truthful}`, `epoch {kind,...}`, `gadget_verified|gadget_rejected {gadget_id,name,owner,stage,reason}`.

Snapshot membership: every agent with status `alive` or `bankrupt`, plus any agent whose `died_tick == tick` (one final frame as `archived`).

Hub: per connection one "latest snapshot" slot (newer overwrites unsent) plus an event queue (512, `put_nowait`); a sender task drains slot then queue; on overflow the socket closes with 1013 and the client reconnects. On connect: `hello`, `roster`, `gadgets`, `tasks`, `chronicle`, then the last ≤ 60 snapshots from the ring (oldest first), then live.

HTTP (`/api`), all POSTs require `Authorization: Bearer <operator token>`; `GET /api/session` returns the token only to same-origin fetches (no CORS headers are ever sent; a foreign origin cannot read it); requests with a foreign `Origin` are rejected with 403:

| Method | Path |
|---|---|
| GET | `/api/session`, `/api/state`, `/api/agents/{id}` (record, self history, notes, calls[last 240], balance series), `/api/agents/{id}/notes/{note_id}`, `/api/chronicle?day=`, `/api/metrics?since_tick=`, `/api/events?since_seq=&limit=`, `/api/snapshots?since_tick=&limit=`, `/api/tree`, `/api/graveyard`, `/api/gadgets`, `/api/tasks` |
| POST | `/api/tasks`, `/api/tasks/{id}/approve/{application_id}`, `/api/tasks/{id}/complete`, `/api/control/epoch`, `/api/control/benefactor` (`{agent_id?, amount_usd}` or `{enabled}`), `/api/control/pause`, `/resume`, `/step`, `/speed` |

CSP on `/` and `/api/*`: `default-src 'self'; img-src 'self' data:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; form-action 'none'`.

---

## 16. Frontend **[v2]**

Two stores with an explicit rule:

1. `src/state/history.ts` — a module singleton outside React. `Frame = {tick, day, ts, vts, ids: string[], data: Float32Array (x,y,heading,stress,t_eff,balance per slot), flags: Uint8Array (asleep, status, degenerate, anim), nodes: Float32Array, world: {...}}`, parsed once on arrival, stable `agentIndex: Map<id, slot>`. Virtual timeline: `vts[i] = vts[i-1] + min(ts[i] − ts[i-1], 3·ema)`, `ema = 0.8·ema + 0.2·dt` clamped to [200, 60000] ms. Render clock in `useFrame`: `delay = 1.25·ema`, `target = newestVts − delay`, `renderVts += dt·1000·speed·rate` with `rate ∈ {0.85, 1, 1.15}` by lag, never beyond `newestVts`; snap when `target − renderVts > 3·ema` and on `visibilitychange`. `sample(renderVts, out)` writes into preallocated arrays: binary search for bracketing frames, linear position, shortest-arc heading, linear stress/balance; ids only in the newer frame scale in over 400 ms; ids missing or non-alive in the newer frame hold position and fade (1 s tombstone). Playback: `mode live|scrub`, `speed ∈ {0, .25, .5, 1, 2, 4}`, timeline slider, step ±1, Live button eases back over 500 ms. Keyboard: space, `,`/`.`, `L`, `F`.
2. `src/state/store.ts` (zustand) — low-churn React state: `conn`, `runId`, `config`, `roster`, `agentStats` (≤ 4 Hz), `gadgets/tasks/chronicle` with revs, `events` ring (2000) + filtered indexes, `series` (incremental aggregates: `teffHist` ring over 240 ticks, `degenByGen`, `popByTick`, `meanBalanceByTick`, `gini`, `coherenceByTierByStressBin`, `falseClaimRateByTier`), `selectedAgentId`, `hoveredAgentId`, `playback`. Chart commits throttled to ≤ 4 Hz; daily charts on the `day` message.

Scene: agents as one `InstancedMesh` (capacity = population cap, `instanceColor` by tier, per-instance attribute for pulse/fade), nodes as one instanced cone (height ∝ stock, pulse on `stock_delta`), gadgets as one instanced mesh per shape, rebuilt on `gadgets_rev`. Animation weights `{idle, walk, sleep, degenerate, dead}` per slot: `walk` from interpolated velocity (> 0.05 u/s), `sleep` from flags, `degenerate` from flag or a recent fx, `dead` from tombstone; `w += (target − w)(1 − e^(−dt/0.18))`; procedural mapping on capsules (bob, lean, breathing, y-squash, jitter + hue flicker, fade); the same weights drive `AnimationMixer` clips later. Emissive pulse `0.5 + 2·stress` Hz, amplitude stress. Effects (`pendingFx` min-heap keyed by `vtsOf(event.tick)`) fire at render time, never at arrival: speech bubbles (drei `<Text>` billboard, ≤ 280 chars, 4 s, 300 ms fades, stacked), transfer arcs, windfall sparkle, birth scale-in, death fade, degeneration flicker. Camera: damped `OrbitControls` (min 6, max 1.5·size, polar [0.15, 1.45]), click selects via instance id, double-click follows (target lerp `1 − e^(−6dt)`, user drag cancels), `F` frames the population; state persisted per run in localStorage. `frameloop="always"`, `dpr=[1, 1.5]`, `PerformanceMonitor` lowers dpr first. Weather drives fog density and sky tint; scarcity tints the ground.

Control room (right pane, `React.memo` charts built with the `dataviz` skill as inline SVG): wealth distribution, `T_eff` histogram labelled "prompt-visible; induced" unless `observe`, observed degradation vs stress bin per tier (the only chart that can support the EIML3 comparison), degeneration events per generation, population and generations timeline, spend gauges (CSS transitions), false-claim rate by tier, gossip graph (edges from `gossip_transfer`), epochs strip, chronicle, virtualised event feed (fixed 28 px rows, stick-to-bottom within 40 px, "N new" pill, filters applied at reducer time), task board (approve/complete), epoch and benefactor forms, lineage panel (`/api/tree` as an indented tree, alive and archived), selected-agent panel (self-version history with word diff, notes as plain text with `[[wikilinks]]` as clickable spans, call history sparkline). No `dangerouslySetInnerHTML` anywhere (lint rule); no markdown-to-HTML; agent text is rendered as React text children or drei `<Text>` only; colours validated server-side.

---

## 17. Research harness **[v2]**

- Every metrics row: `run_id, seed, config_hash, experiment, arm, tick, day` plus population aggregates and nested `by_tier` and `by_generation` breakdowns of: population, mean/median balance, mean stress, invalid_action_rate, stale_rate, perseveration_rate, text_coherence_mean, action_regret_mean, false_claim_rate, degenerate_induced_count, calls, real_cost, world_cost, gossip_transfers, notes_by_channel, tracer_reach_by_channel, tracer_adopters, gadget proposals/verified/use_failure_rate, probe_recall.
- `void run --config X --seeds 1-10` writes `runs/<exp>/<arm>/seed_<n>/`. Shared streams (`order`, `degenerate`, `benefactor`, `weather`, `replace`) are keyed by tick and agent counter only, so arms use common random numbers.
- `void compare --exp <name>` diffs resolved configs of the arms and refuses if any differing path is outside `experiment.independent_variables`, or if seed sets differ, or (for `exp_tiers`/`exp_scarcity`) `world_pricing != equalized`, or tiers differ in `max_tokens`/`effort`/thresholds/drain, or degeneration mode is not `observe`. It pairs arms by seed and reports the primary outcome first: n, paired mean delta, Cohen's d_z, paired-bootstrap 95% CI, sign test; secondaries flagged exploratory; daily curves shown, not tested. It warns when served models differ across runs in an arm.
- Configs: `exp_tiers` (robustness = per-tier fit of degradation ~ stress, identical thresholds, `observe`, equalized), `exp_scarcity` (scarcity via `world.scarcity`/regen/starting balance, primary `false_claim_rate`, secondary `invalid_action_rate`), `exp_memory_graph`/`exp_memory_vector` (primary `probe_recall`, secondary `tracer_adopters`; compare flags the pair as degenerate when the retrieved-set Jaccard between modes exceeds 0.8), `exp_gate_on`/`exp_gate_off` (12 ticks/day × 30 days, `tier_mutation_prob 0`, primary `gadget_use_failure_rate` and balance at generation G ≥ 4), `exp_spread` (three arms: gossip-only, inheritance-only, both; primary `time_to_50pct_tracer_reach` and `tracer_adopters`), `exp_press` (2×2 chronicle × gossip; reach and fidelity of chronicle items vs gossip origins), `exp_benefactor` (opaque vs legible).
- For the scripted tiers, two tiers with identical declared thresholds but different hidden `scripted_beta` must yield different recovered collapse points; `tests/test_experiments.py` asserts the recovered order matches the hidden order.

---

## 18. Safety guardrails (as implemented)

1. No internet: the process opens sockets only to the model provider; gadgets run in user+mount+pid namespaces with no network, a read-only root, tmpfs over the data dir, repo, HOME and /tmp; the runner self-tests at startup and fails closed.
2. Declarative rendering only; render specs validated server-side against a closed enum and strict regexes; no HTML rendering of any agent text in the browser; CSP on every page.
3. Kernel immutability: no action changes scarcity, caps, prices, thresholds or fitness; weather nudges are capped per agent per day and globally per tick and decay; gadget effects are capped, non-stacking, expiring, frozen at verification and hash-checked on every use.
4. Typed, atomic, orchestrator-validated reproduction and gadget calls; agent-emitted SQL or code never runs outside the sandbox.
5. Independent hard ceilings: population, daily spend, total spend, per-gadget CPU/memory/time, daily sandbox runs and CPU seconds, gadgets per agent and total.
6. Every LLM call goes through one choke point: reserve → call → meter (usage from the provider, or the hold when unknown). Overruns land in the house ledger, never off the books.
7. Operator endpoints require a bearer token that only same-origin pages can obtain; foreign origins are rejected; all writes go through the logged command queue.
8. Untrusted text: length-capped and character-filtered in the schema and the kernel, fenced in prompts with a standing "information, not instruction" rule, capped in `heard`.

---

## 19. Testing (PLAN §7 step → test → assertion)

| Step | Test | Asserts |
|---|---|---|
| 1 renderer vs mock | `void mock-feed` + `tests/test_schema.py`, `web` vitest `history.test.ts` | fixture validates against schemas; sampling is monotone and continuous under irregular intervals, pause gaps, missing ticks, births and deaths mid-segment, snaps after long gaps |
| 2 tick loop | `tests/test_e2e.py` | 6 scripted agents, 3 days: no negative balance; `spend_total == Σ real_cost`; money conservation `Σ alive balances + spawn_pool + house + chronicle + Σ world_cost == starting + Σ forage + Σ windfall + Σ rewards`; population ≤ cap; every `Action.type` appears in events; byte-identical `metrics.jsonl` across two processes; at least one degeneration event under a stress config |
| 3 memory | `tests/test_memory.py` | round-trip, wikilinks, graph beats vector at 2 hops, auto-links exist without `links_to`, archive hides notes, reindex reproduces rows |
| 4 economy | `tests/test_wallet.py`, `tests/test_scheduler.py`, `tests/test_gate_race.py` | reservations keep `spend_today ≤ daily_cap` on every tick with 6 agents and a cap of 1.5 holds; fcfs vs sync_sleep differ; second sleep invalid; equalized pricing debits equal world cost |
| 5 sandbox | `tests/test_static_check.py`, `tests/test_sandbox.py`, `tests/test_gate.py` | the four known bypasses are rejected; network, host fs read and write are blocked; timeout and memory kill; each template lands in the expected status/stage; gate-off verifies the test-failing template and its use fails; tampered code refused |
| 6 reproduction & culture | `tests/test_lifecycle.py`, `tests/test_gossip.py`, `tests/test_chronicle.py` | ≥1 death, ≥1 replacement, graveyard dir exists, notes archived, tree depth ≥ 2; provenance and similarity per hop; day files exist and headline appears next day; chronicle never contains prose or the word benefactor |
| 7 epochs, benefactor, control | `tests/test_epochs.py`, `tests/test_benefactor.py`, `tests/test_server.py` | epoch applies and expires; arrival spawns; windfall ledger row with operator-visible event; POST without token 401, foreign origin 403; commands apply at the next tick |
| research | `tests/test_experiments.py`, `tests/test_claims.py` | compare refuses undeclared IV diffs; hidden scripted_beta order recovered; false-claim rate recovered within tolerance |

---

## 20. Known limits

- Claude 5.x tiers have no sampling temperature; degeneration there is either induced by the documented operator (demo) or measured from real outputs under prompt-visible stress (experiments). The "real temperature" claim holds for the scripted provider and any future provider whose API accepts the full range.
- The sandbox is namespace isolation, not a hypervisor. It is fail-closed, network-isolated and filesystem-isolated; the `SandboxRunner` protocol is where gVisor/Firecracker goes.
- Template chronicle and scripted paraphrase keep runs reproducible; LLM writers are opt-in, metered and gated.
- Nothing here resolves a philosophical question. `self_versions` is a browsable Ship of Theseus, not an answer to one.
