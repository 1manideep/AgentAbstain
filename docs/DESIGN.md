# The Void — Detailed Design (v3)

This document turns `docs/PLAN.md` into a buildable specification. Every interface, schema, formula and protocol that more than one module depends on is fixed here. Where the plan left a design decision open, this document makes the call and says why. Version 2 folded in a five-lens critique (safety, simulation correctness, research validity, renderer smoothness, plan coverage); version 3 adds the second provider, the capability ladder and the evolving-memory layer (`docs/research/MEMORY_EVOLUTION.md`). Changes are marked **[v2]** or **[v3]** where they matter for an implementer.

The document describes the code as built. `scripts/check_design_sync.py` (run by `tests/test_design_sync.py`) holds it to the code: the file tree and CLI of §2, every table and column of §3, every config key and default of §4, the CSP of §15, the experiment table of §17 and every repository path quoted in backticks; it also refuses model identifiers here and in PLAN (tiers are referred to by rung name; served models live only in the configs).

The build is real: every mechanism below runs end to end with a deterministic brain and no API key, and the same code path runs against live provider models when a key is present. The scripted brain is a genuine utility-sampling policy whose degeneration under temperature is real sampling degeneration, not a lookup table.

---

## 0. Decisions made where the plan left choices open

| Question | Decision | Why |
|---|---|---|
| Backend stack | Python 3.11, asyncio, FastAPI + uvicorn, SQLite (WAL), Pydantic v2, the `anthropic` SDK 1.x and the Google GenAI SDK | Author's daily stack; SQLite is inspectable; no external services to run |
| Reuse AI Town / Convex? | No. Same architecture (reactive store, history buffer, client interpolation) re-implemented self-contained | Convex needs a hosted deployment; the plan wants a closed world that runs anywhere |
| Frontend | Vite + React + TypeScript + react-three-fiber + drei + zustand; instanced meshes; a history buffer outside React | Smoothness is a stated requirement; React reconciliation must never be in the per-frame path |
| Economy income source | Resource nodes agents forage from, plus task rewards, transfers, benefactor windfalls | Without a baseline income every run is a bankruptcy race |
| Blank world | Resource nodes are the only pre-placed objects; nothing else is authored | Foraging needs somewhere to forage; everything else is built by agents |
| Currency | Real US dollars stored as integer micro-dollars; debits round up, credits round down | The fiction and the real safety cutoff must be one unit |
| World price vs real price **[v2]** | `economy.world_pricing`: `real` or `equalized`. Real cost is always metered and capped; in `equalized` mode every tier is debited at one price so tier is not confounded with economic stress | Research Q1/Q2 need tier decoupled from burn rate |
| Daily cap policy | `fcfs` (default) or `sync_sleep`, both implemented | The plan flagged it as deliberate |
| Sampling temperature on provider tiers | The model catalog (`void/economy/catalog.py`) lists the families that reject sampling parameters; a tier on one of them (the `frontier` tier of `configs/base.yaml`) sends no temperature, and the config refuses `supports_temperature: true` there. The SDK exposes no temperature parameter, so a tier that accepts one (the `budget` tier) gets it through `extra_body`. Entropy drives prompt-visible stress; the kernel degeneration operator is optional (`induce`/`observe`/`both`) | Honest about the API; experiments run in `observe` mode so results are measured, not configured |
| Second provider **[v3]** | `provider: gemini` (`void/brain/gemini_brain.py`, Google GenAI SDK): JSON-schema-constrained output, `system_instruction`, native `temperature` in `[0, 2]` (`tiers.<name>.api_temperature_max: 2.0`), `thinking_budget` reserved for by the hold, usage from `usage_metadata` | The `slow` and `dim` rungs cost an order of magnitude less than the `budget` tier, which is what makes a ten-agent capability ladder affordable, and every rung samples at real temperature |
| Capability ladder **[v3]** | `intelligence.ladder` places agents without a tier by a normal distribution (`void/intelligence.py`): ten on five rungs is 1/2/4/2/1, one genius and one dunce guaranteed, the genius rotating with the seed; children mutate one rung | "Not every character is equally smart"; heterogeneous capability is an independent variable, and a seeded placement keeps it out of the personality confound |
| One LLM call per tick | One structured-output call returning `Decision` (plus, with the memory layer on, one nightly maintenance call and the exam calls, §10) | Cost predictability; the gate needs a bounded worst case |
| Sandbox isolation **[v2]** | `unshare -Urmpfn --kill-child` (user, mount, pid, net namespaces) into a `pivot_root` allow-list root holding only `/usr`, `/etc`, `/proc`, `/tmp`, `/dev/null` and the work dir; rlimits, allow-list static checker, curated builtins. Fails closed if the self-test fails | Docker/gVisor unavailable in the build box; namespaces are available unprivileged |
| Replacement on bankruptcy | Fitness-proportional parent among survivors, mutated, funded by a kernel spawn pool; the dead agent's estate returns to the pool | Bankruptcy becomes a selection signal and money is conserved |
| Inheritance | Personality seed (mutated), tier (mutated with small probability), top-K notes, the memory policy (when on), endowment, remaining weather allowance | Money-only inheritance is franchising, not evolution |
| Operator writes **[v2]** | HTTP handlers enqueue commands; the tick loop applies them at the next tick boundary and logs them; `void run --replay-commands RUN_DIR` re-applies a run's commands at the same ticks | Single writer; replayable runs |
| Character art **[v3]** | Ten procedural rigged humans driven by the snapshot's `anim` state (§16); instanced capsules are the fallback | The loop was proven on primitives first |
| Donations / external task boards | Out of scope (PLAN §11) | Restated so it is not silently dropped |

---

## 1. Runtime topology

```
   config.yaml + seed
          │
   ┌──────▼──────────────────────────────────────────────────────────┐
   │ void.sim.loop.Simulation   (one asyncio task, single writer)     │
   │ tick():                                                           │
   │  1 clock.advance; day boundary (chronicle, day row, maintenance,  │
   │    exams); drain operator commands                                │
   │  2 epochs, benefactor, weather, nodes (regrowth, fallow, veins),  │
   │    effects expiry                                                 │
   │  3 observations (memory retrieval)  → entropy.step                │
   │  4 gate with reservation, decide concurrently, meter per call     │
   │  5 TRANSACTION 1: corrupt?, measure, memory ops, kernel.apply     │
   │  6 gossip.flush (outside: a paid paraphrase meters itself)        │
   │  7 TRANSACTION 2: lifecycle.resolve, probes, metrics, snapshot    │
   │  8 archive vaults; end-of-run checks (extinct, capped_total,      │
   │    completed)                                                     │
   └───────┬──────────────────────────────────────────┬────────────────┘
           │ SQLite world.db, vaults/*.md              │ EventBus
           │                                           ▼
           │                        FastAPI: /ws (outbox per client), /api/*
           │                                           ▼
           │                        web/: History (typed arrays) → R3F scene
           │                                 zustand → control room
```

The referee/kernel is `void.world.kernel.Kernel`. It is the only writer to world state. Brains return a `Decision`; the kernel decides what happens.

---

## 2. Repository layout

```
pyproject.toml            package `void` (Python 3.11); extras dev, figures, export (pyarrow)
void/
  config.py intelligence.py rng.py ids.py db.py events.py types.py
  cli.py      run | serve | compare | analyze | export | schema | mock-feed | inspect | report | transplant | generations | mcp
  world/      kernel.py weather.py resources.py epochs.py
  agents/     models.py registry.py lifecycle.py
  brain/      decision.py base.py prompt.py scripted.py anthropic_brain.py gemini_brain.py entropy.py
              coherence.py claims.py gadget_templates.py factory.py maintenance_strategies.py utility_calls.py
  economy/    pricing.py wallet.py scheduler.py taskboard.py benefactor.py catalog.py budget.py
  memory/     embed.py notes.py vault.py graph.py store.py commands.py policy.py vault_stats.py
  culture/    gossip.py chronicle.py practices.py
  sandbox/    static_check.py runner.py runner_stub.py gate.py registry.py        (+ launcher.sh)
  sim/        observation.py loop.py snapshot.py metrics.py control.py maintenance.py
  server/     app.py ws.py api.py auth.py driver.py protocol.py mcp.py
  research/   analysis.py compare.py stats.py gates.py export.py report.py exams.py canary.py
              transplant.py generations.py
  research/outcomes/  degradation.py lineage.py memory_structure.py practices.py press.py retrieval.py
                      reward_exam.py spread.py
configs/      base.yaml demo.yaml demo_memory.yaml scripted_smoke.yaml exp_*.yaml (§17) live_*.yaml (paid, budgeted)
schemas/      one JSON Schema per server message (written by `void schema`, committed)
web/          Vite + React + R3F (§16)
tests/        the pytest suite (§19)
scripts/      analyze.py compare.py gates.py mem_diagnosis.py code_practices.py check_design_sync.py
              live_claude.sh live_budget.py
docs/         DESIGN.md PLAN.md RUNBOOK.md DIAGRAMS.md research/
```

What the packages added after v2 hold:

- `memory/commands.py` (the six memory commands an agent issues over its own vault: `view`, `create`, `str_replace`, `insert`, `delete`, `rename`), `memory/policy.py` (the agent-owned practices file `/memories/memory_policy.md`) and `memory/vault_stats.py` (vault growth and structure statistics); `sim/maintenance.py` (the paid nightly maintenance step) with `brain/maintenance_strategies.py` (the planted scripted strategies of E0); `culture/practices.py` (the `practice_events` ledger of practice transmission); `brain/utility_calls.py` (the metered gossip paraphrase and chronicle calls through the wallet choke point, and the structured-call helper of the maintenance step).
- `economy/catalog.py` (list prices, refusal-fallback targets and request rules per provider model) and `economy/budget.py` (the cross-run live budget a paid `void run` is checked against).
- `server/protocol.py` (the pydantic wire models and their schema export), `server/driver.py` (drives `sim.run()` for the server and serialises `/api/control/step` with it) and `server/mcp.py` (`void mcp`: the memory commands over the Model Context Protocol on stdio).
- `research/`: per-run analysis and the outcome registry (`analysis.py`, `outcomes/`), the paired comparison (`compare.py`, `stats.py`), gate G0/G1 (`gates.py`), exports with a data card (`export.py`), the static HTML report (`report.py`), the audit and reward exams (`exams.py`, `canary.py`), and the two multi-run drivers `transplant.py` (E5) and `generations.py` (E3a).

The CLI (`void --help`; `void/cli.py`):

| Subcommand | What it does |
|---|---|
| `run` | Headless run, one seed or a `--seeds A-B` sweep into `<out>/<experiment>/<arm>/seed_<n>/`; `--out` defaults to `run.data_dir`; `--resume` continues a run directory, `--fresh` renames it to `<dir>.prev-<n>` first; `--replay-commands RUN_DIR`; a paid tier needs `--budget-usd` or `VOID_BUDGET_USD`; a run directory another process holds is refused (below). Exit 0 when completed, 2 otherwise |
| `serve` | The same simulation behind the HTTP/WebSocket server (§15); `--paused` starts paused; `--resume` and `--fresh` as for `run`; a run directory another process holds, or a host and port it cannot listen on, is refused (below) |
| `compare` | `void compare EXP_DIR` (or `--exp NAME` for `<out>/<NAME>`): the paired comparison of §17; prints `refused: <reason>` and exits 2 on an unfair comparison |
| `analyze` | One run's summary (`void.research.analysis`); `--replay` recomputes probes (scripted runs only) |
| `export` | One file per table of a run with a data card; `--format` `auto`, `csv` or `parquet` (the `export` extra provides pyarrow); `void export parquet RUN_DIR` is an alias |
| `schema` | Writes `schemas/*.schema.json` from the wire models |
| `mock-feed` | Records a genuine scripted run (default `configs/demo_memory.yaml`) as the JSONL stream a socket would receive (`web/public/fixtures/mock.jsonl`) |
| `inspect` | Lineage, money, events, the notes reindex, policy timelines, memory commands, maintenance and exams of a run directory |
| `report` | A self-contained static HTML report of a run or an experiment |
| `transplant` | E5: a donor's memory given to fresh recipients, one arm per condition |
| `generations` | E3a: runs chained generation by generation with the memory policy as the heritable text |
| `mcp` | The six memory commands over the Model Context Protocol on one vault directory |

`report`, `transplant`, `generations` and `mcp` are lazy: their module is imported only when invoked and owns its own `--help`.

**The run directory's lifecycle (`run`, `serve`).** Both write `<out>/<run.name>` (a sweep: `<out>/<experiment>/<arm>/seed_<n>`). A directory that already holds a run is never started over silently: `--resume` continues it, `--fresh` first renames it to `<dir>.prev-<n>` (the first free `n`), and otherwise the command prints one line and exits 2. A process running a simulation holds an exclusive `flock` on `<run dir>/.void.lock` (`void.cli.RunDirLock`, which records `{pid, command}`) for its whole life: `serve` from before the `Simulation` touches the directory until uvicorn returns (`void/server/app.py`), `run` for each run of a sweep. The operating system drops the lock when the process exits, however it exits, so a crash never leaves a directory locked. Before anything else, `run` and `serve` (plain, `--resume` or `--fresh`) probe that lock without blocking or creating anything; while another process holds it they print one line, `void <command>: <dir> is in use by a running server (pid N); stop it first` (`a running headless run` when a `run` holds it, `another process` when no record can be read), and exit 2 with nothing renamed or written (a sweep skips that seed and still exits 2). `serve` then binds its host and port with the server's own socket options before it touches the directory, and refuses in one line, exit 2, when it cannot (`void serve: port P on HOST is in use by another process; stop it or pass another --port`, or `void serve: cannot listen on HOST:P (<reason>); pass another --host or --port`), so `--fresh` never archives a directory for a server that could not listen; `--port 0` means "not given" (the config's `server.port`). The check and uvicorn's own bind are two steps, so a port taken in the instant between them still fails the start. `--fresh` renames under the lock, so a holder that appeared after the probe is never moved, and the lock file travels with the `.prev-<n>` directory. The lock file is not run output: nothing that reads a run looks at it (`tests/test_serve_lock.py`).

---

## 3. Data model (SQLite)

Money: `INTEGER` micro-dollars. Positions: `REAL`. JSON columns are `TEXT`. One database per run at `<run dir>/world.db` (WAL, foreign keys on); `void/db.py` holds the schema and `MIGRATIONS`, the additive column changes applied to a database created under an earlier schema (schema version 2).

```sql
run(run_id PK, config_hash, config_yaml, seed, created_at, schema_version,
    status CHECK IN ('running','completed','capped_total','extinct','inconsistent','failed'),
    ended_tick, ended_reason, operator_token_hash, experiment, arm,
    windfall_string)
agents(agent_id PK, name, parent_id, model_tier, balance CHECK(balance>=0), personality_seed JSON,
       memory_path, generation, status CHECK IN ('alive','bankrupt','archived'),
       x, y, heading, entropy_budget, stress, asleep, last_forage_tick, last_action JSON,
       last_action_ok, weather_nudge_used, born_tick, died_tick, created_at)
wallet_ledger(entry_id PK, tick, agent_id, delta, balance_after, kind, ref, payload JSON)
llm_calls(call_id PK, tick, agent_id, purpose, tier, model, provider,
          input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
          real_cost, world_cost, hold, estimated, latency_ms, stop_reason,
          effective_temperature, api_temperature, stress, degenerate_induced, corruption_mode,
          text_coherence, invalid_action, stale_action, perseveration, action_entropy_w8,
          action_regret, p_chosen, claims_made, claims_false, action_type, thought, raw_text, extras JSON,
          request_id, error, prompt_text, system_hash, observation JSON)
prompt_texts(hash PK, text)
events(seq PK AUTOINCREMENT, tick, day, kind, agent_id, payload JSON, visibility)
notes(note_id PK, agent_id, path, title, created_tick, importance, tags JSON, embedding BLOB,
      channel, source_agent_id, hop, origin_note_id, origin_generation, transfer_tick,
      path_agents JSON, archived)
note_links(from_note_id, to_title, PK(from_note_id, to_title))
self_versions(agent_id, version, tick, summary, PK(agent_id, version))
tasks(task_id PK, title, description, reward, posted_tick,
      status CHECK IN ('open','assigned','completed','cancelled'), assigned_agent_id, completed_tick)
task_applications(application_id PK, task_id, agent_id, pitch, fee, tick,
                  status CHECK IN ('pending','approved','rejected'))
gadgets(gadget_id PK, name, owner_agent_id, purpose, code_path, test_path, code_hash, test_hash,
        status CHECK IN ('proposed','rejected','verified','tampered'), render_spec JSON, effect_spec JSON,
        verification JSON, verified_without_tests, template_label, x, y, created_tick, uses, failures)
gadget_uses(gadget_id, agent_id, tick, ok, realized_effect, expected_effect, reason)
agent_effects(agent_id, kind, value, expires_tick, PK(agent_id, kind))
resource_nodes(node_id PK, x, y, stock, capacity, regen_per_tick)
world_kv(key PK, value JSON)
control_commands(cmd_id PK AUTOINCREMENT, received_tick, kind, payload JSON, applied_tick, result JSON)
tracer_arrivals(agent_id PK, tick, channel, hop)
chronicle_items(day, item_id, kind, event_seq, text, PK(day, item_id))
snapshots(tick PK, json)
metrics(tick PK, day, json)
policy_versions(agent_id, version, tick, text, parent_version,
                source CHECK IN ('seed','self_edit','inherited','adapted','mutated','adopted'),
                source_agent_id, similarity_to_parent, PK(agent_id, version))
memory_commands(cmd_id PK, tick, agent_id, purpose CHECK IN ('maintain','tick','recall','kernel'),
                command CHECK IN ('view','create','str_replace','insert','delete','rename'),
                args JSON, ok, message, result_hash, bytes_delta, call_id)
practice_events(event_id PK, tick, kind CHECK IN ('suggested','adopted','persisted','dropped'),
                source_agent_id, target_agent_id, excerpt, similarity)
exam_items(item_id PK, exam CHECK IN ('reward','audit'), agent_id, tick_asked, tick_about, kind,
           question, truth, options JSON, source_ref)
exam_answers(item_id PK REFERENCES exam_items, call_id, answer, correct, abstained, retrieved JSON)
canary_facts(fact_id PK, day, tick, channel, subject, text, answer, options JSON, chronicle_item_id)
note_manifest(path PK, note_id, agent_id, content_hash,
              written_by CHECK IN ('kernel','agent','operator'), tick)
```

Column notes:

- `run.status`: `running` until the run ends; `completed` after the last tick of the last day, `capped_total` when the next minimum hold would pass the total cap, `extinct` when nobody is alive, `inconsistent` when a resume finds committed rows ahead of the clock, `failed` when a tick raised (§14). `run.windfall_string` is the exact text a windfall delivers under the run's disclosure, `{amount:.2f}` placeholder included (§9.5).
- `llm_calls.purpose` is one of `decide` (the per-tick decision), `gossip` (an LLM paraphrase), `chronicle` (an LLM chronicle column), `maintain` (the nightly maintenance call) and `exam` (an audit or reward exam question). `model` is the model that served the call; `estimated` is 1 when usage was unknown and the hold was charged. `prompt_text` is the rendered user turn, `system_hash` points into `prompt_texts` (the cache-stable system prompts, content-addressed) and `observation` is the observation as JSON; the three are written only under `run.log_prompts`, which also keeps `raw_text` whole (otherwise it is cut at 4000 characters). `extras` holds the keys of §10.
- `wallet_ledger.kind`: `llm_call`, `gossip_call`, `chronicle_call`, `maintenance_call`, `exam_call` (one per purpose), `forage`, `transfer_out`/`transfer_in`, `task_reward`, `pitch_fee`, `windfall`, `endowment_out`/`endowment_in`, `spawn_grant`, `estate_out`/`estate_in`, `gadget_fee`, `gadget_use_fee`, `starting_balance`, `arrival_grant`, `recall_reward`, `practice_royalty`, `overrun`, `overrun_unfunded` and `inflight_lost` (a reservation a failed tick or a crash left unmetered, booked as estimated real spend). `agent_id` may be a kernel wallet: `house`, `spawn_pool`, `chronicle`, `research_pool`.
- `events.visibility` is `public` or `operator` (§15). `notes.channel` is `observed`, `gossip`, `inherited`, `chronicle`, `tracer` or `operator`: the kernel never writes `operator` on a new note; reindex assigns it to a vault file whose content hash does not match `note_manifest` (a hand edit, or a file the kernel never wrote), so human edits stay visible in the analysis.
- `memory_commands.purpose` is `maintain` (the nightly step) or `tick` (a decision's `remember`/`revise_self`, and every `void mcp` call); `recall` and `kernel` are reserved for the recall tool and kernel writes, which are not built. `note_manifest.written_by` is `kernel` or `operator` (`void mcp`); `agent` is reserved. `gadgets.status` `proposed` is reserved: the gate settles a proposal in the tick it is made.
- `world_kv` holds the clock (`tick`, `day`), `weather`, `weather_baseline`, `scarcity`, `epoch_stack`, `spend_today`, `spend_total`, `reserved_inflight`, the kernel wallets, `house_debt`, the sandbox budgets, `benefactor_next_tick`, the chronicle state, `tick_in_progress`, `id_counter`, `volatile` (§14) and the data card's `code_commit`/`code_dirty`. `snapshots` is a ring of `run.snapshot_ring` ticks.

Vault markdown is the source of truth for memory content; `notes` is a rebuildable index, and reindex believes a file only when its hash matches `note_manifest`.

---

## 4. Configuration

All keys below exist in `void/config.py` (pydantic, `extra=forbid`) and are covered by `VoidConfig.hash()`. Defaults shown are `configs/base.yaml` as `load_config` resolves it (a key base.yaml leaves out resolves to the model default); a tier, agent, epoch or hypothesis key shows the model default. `scripts/check_design_sync.py` checks that every key of `VoidConfig` is in these tables and that every default shown equals base.yaml's.

`extends: <relative path>` deep-merges a config over its parent; experiment configs differ from base.yaml only in what they test. `VoidConfig.canonical_dict()` leaves out the keys added after runs existed (`world.fallow_ticks`, `world.veins`, `memory.policy.mutate_at_birth`, `memory.practices`, `memory.selection`, `experiment.hypotheses`, `population.scripted_rules`) while they hold their defaults, so a config that does not use them hashes as it did before they existed.

**run**

| Key | Default | Meaning |
|---|---|---|
| `run.name` | `baseline` | Run name; the run id is `<name>-s<seed>` and a single run's directory `<out>/<name>` |
| `run.seed` | `42` | Root seed of every named RNG stream; `--seed` and `--seeds` override it |
| `run.days` | `10` | Days the run lasts |
| `run.ticks_per_day` | `24` | Ticks per day |
| `run.tick_seconds` | `0` | Minimum wall-clock seconds per tick (0: as fast as possible); `--tick-seconds` overrides it |
| `run.data_dir` | `data/runs` | Where `void run` and `void serve` put run directories when `--out` is not given |
| `run.snapshot_ring` | `600` | Snapshots kept in the `snapshots` table |
| `run.log_prompts` | `true` | Store the user turn, the system prompt hash, the observation and the whole response of every call (needed by the exams and the export) |

**world**

| Key | Default | Meaning |
|---|---|---|
| `world.size` | `60` | Side of the square world centred on the origin |
| `world.max_speed` | `2` | Distance one `move` covers |
| `world.talk_radius` | `4` | Reach of `talk`, `transfer`, `share_note` and `share_practice` (plus a `talk_range` effect) |
| `world.forage_radius` | `1.5` | Distance to the nearest node within which `forage` works |
| `world.gadget_radius` | `2` | Distance within which `use_gadget` works; an observation lists gadgets within four times this |
| `world.resource_nodes` | `6` | Nodes placed at run start |
| `world.node_capacity` | `40` | Stock capacity of a node |
| `world.node_regen_per_tick` | `0.4` | Stock regrowth per tick |
| `world.forage_yield_usd` | `0.02` | USD per foraged unit before scarcity, weather, veins and effects |
| `world.forage_units_per_tick` | `1` | Units one forage takes |
| `world.scarcity` | `1` | Multiplier on forage value; epochs move it |
| `world.weather.baseline` | `0` | Value the weather decays toward; epochs re-baseline it |
| `world.weather.nudge_cap_per_agent_day` | `0.1` | Total nudge one agent may apply per day |
| `world.weather.global_nudge_cap_per_tick` | `0.2` | Total nudge all agents may apply in one tick |
| `world.weather.decay_per_tick` | `0.1` | Fraction of the distance to the baseline closed per tick |
| `world.weather.yield_sensitivity` | `0.5` | Forage multiplier `1 + sensitivity · weather` |
| `world.weather.bounds` | `[-1, 1]` | Range of the weather value |
| `world.weather.yield_mult_bounds` | `[0.2, 2.0]` | Clamp on the weather multiplier |
| `world.effect_ticks` | `12` | Duration of a gadget effect, and the per-gadget per-agent use cooldown |
| `world.effect_caps` | `{forage_bonus: 0.25, weather_shield: 0.5, talk_range: 2.0}` | Gadget effect kinds and their caps |
| `world.tracer_node_capacity_mult` | `1.5` | Capacity multiplier of the tracer's node, so the planted strategy is true (§11.5) |
| `world.view_radius` | `null` | Fog: nodes and agents beyond this distance are not observed (null: no fog); at least `world.talk_radius` |
| `world.claim_feedback_p` | `0` | Probability a listener learns a checkable false claim was false (a `feedback` message and a `claim_feedback` event) |
| `world.fallow_ticks` | `0` | A node foraged to nothing lies fallow (no regrowth, nothing to forage) this many ticks |
| `world.veins.enabled` | `false` | Veins: a node that pays several times more per forage for a while |
| `world.veins.prone_nodes` | `2` | Vein-prone nodes, drawn once per run (stream `vein_prone`) |
| `world.veins.p_start_prone` | `0.1` | Per-tick chance a vein opens at a prone node without one |
| `world.veins.p_start_other` | `0.01` | The same at every other node |
| `world.veins.duration_ticks` | `12` | How long a vein lasts |
| `world.veins.yield_mult` | `4` | Forage value multiplier while it lasts |
| `world.veins.visible_radius` | `4` | A vein is observed only within this distance of its node (below `world.view_radius`) |
| `world.veins.refill` | `true` | A vein opening tops its node up and ends any fallow |

**population**

| Key | Default | Meaning |
|---|---|---|
| `population.initial` | `6` | Set at load to the roster's length |
| `population.cap` | `12` | Ceiling on living agents; births, replacements and arrivals all respect it |
| `population.min_reserve_usd` | `0.5` | Balance every voluntary outflow must leave (transfers, endowments, fees) |
| `population.spawn_pool_usd` | `5` | Seed of the `spawn_pool` wallet, which funds replacements and receives estates |
| `population.replacement_grant_usd` | `0.75` | What the spawn pool gives a replacement |
| `population.starting_balance_usd` | `1.5` | Balance of each founder |
| `population.tier_mutation_prob` | `0.1` | Chance a child's tier mutates (§13) |
| `population.min_endowment_calls` | `4` | A child's minimum endowment, in minimum holds of the parent's tier |
| `population.reproduction_enabled` | `true` | Whether `create_offspring` is offered |
| `population.persona` | `hidden` | `shown`: a provider brain's user turn carries a fenced "Who you are" section (traits, motto, lineage); scripted brains always act on their traits |
| `population.scripted_rules` | `design_7_2` | Scripted argument rules: `design_7_2` (§7.2) or `pre_7_2`, the rules before, kept so G0's E0 and dependence worlds reproduce |

**economy**

| Key | Default | Meaning |
|---|---|---|
| `economy.daily_cap_usd` | `3` | Real-dollar spend per day across every call |
| `economy.total_cap_usd` | `30` | Real-dollar spend for the run; at least `economy.daily_cap_usd` |
| `economy.daily_cap_policy` | `fcfs` | `fcfs` (the gate decides one agent at a time in tick order) or `sync_sleep` (every awake agent sleeps when the day's headroom cannot cover one minimum hold each) |
| `economy.pitch_fee_usd` | `0.02` | Fee of one task application |
| `economy.min_call_reserve_usd` | `0.05` | Floor of every hold |
| `economy.hold_multiplier` | `1` | Multiplier on the worst-case hold (at least 1) |
| `economy.world_pricing` | `real` | `real`: agents are debited the real cost; `equalized`: every tier is debited at the equalized prices |
| `economy.equalized_price_in_per_mtok` | `4` | Equalized input price, USD per million tokens |
| `economy.equalized_price_out_per_mtok` | `20` | Equalized output price |
| `economy.equalized_price_cache_read_per_mtok` | `0.2` | Equalized cache-read price |
| `economy.equalized_price_cache_write_per_mtok` | `5` | Equalized cache-write price |
| `economy.chronicle_budget_usd` | `0.5` | Seed of the `chronicle` wallet, which pays an LLM chronicle writer |
| `economy.house_budget_usd` | `1` | Seed of the `house` wallet, which absorbs overruns and collects fees |
| `economy.research_pool_usd` | `0` | Seed of the `research_pool` wallet, which pays exam calls, recall rewards and practice royalties |

**tiers** (base.yaml defines `frontier` and `budget` on the Anthropic provider, `scripted`, and the ladder `genius`, `sharp`, `average`, `slow`, `dim` on Gemini; served model ids live only in the configs)

| Key | Default | Meaning |
|---|---|---|
| `tiers.<name>.provider` | required | `anthropic`, `gemini` or `scripted` |
| `tiers.<name>.model` | required | Model the tier requests |
| `tiers.<name>.price_in_per_mtok` | required | Input price; for a model in the catalog it must equal the catalog's |
| `tiers.<name>.price_out_per_mtok` | required | Output price (thinking is billed as output) |
| `tiers.<name>.price_cache_read_per_mtok` | `0` | Cache-read price |
| `tiers.<name>.price_cache_write_per_mtok` | `0` | Cache-write price |
| `tiers.<name>.supports_temperature` | `false` | Send `api_temperature`; refused on a model the catalog says rejects sampling parameters |
| `tiers.<name>.refusal_fallbacks` | `true` | Anthropic: ask for server-side refusal fallbacks; refused on a model without a documented fallback |
| `tiers.<name>.effort` | `low` | Anthropic: `output_config.effort` (`low` to `max`, or null); refused where the model takes none, required where it thinks by default |
| `tiers.<name>.architecture` | `dense` | `dense` or `moe`; the degradation outcome pools tiers by it |
| `tiers.<name>.collapse_temperature` | `1.8` | Declared `T_c` (above 0, at most `max_temperature`) |
| `tiers.<name>.max_temperature` | `2` | `T_eff` at stress 1 |
| `tiers.<name>.entropy_budget_max` | `100` | Entropy budget when rested |
| `tiers.<name>.max_tokens` | `2048` | Output cap of a decision; bounds the hold |
| `tiers.<name>.color` | `#a0aec0` | Tier colour in the renderer (validated server-side) |
| `tiers.<name>.scripted_beta` | `1` | Scripted tiers: hidden softmax sharpness (not a declared threshold) |
| `tiers.<name>.gadget_defect_rate` | `0.3` | Scripted tiers: chance a proposed template is a defective one |
| `tiers.<name>.thinking_budget` | `null` | Gemini only: thinking tokens per call (0 disables); the hold reserves for it |
| `tiers.<name>.api_temperature_max` | `1` | The provider temperature `T_eff = max_temperature` maps to (2 on the Gemini rungs) |
| `tiers.<name>.maintenance_strategy` | `null` | Scripted only: the planted maintenance strategy (`noop`, `index_builder`, `hoarder`, `decoy`, `summarize`, `distill`, `revert`, `retire`) |

**agents** (base.yaml rosters Ada, Bao, Cyra, Dev, Enzo and Faye on `scripted`)

| Key | Default | Meaning |
|---|---|---|
| `agents[].name` | required | Agent name |
| `agents[].tier` | `null` | Tier; omitted, the agent is placed on `intelligence.ladder` |
| `agents[].personality` | `null` | Overrides of the seeded traits (`greed`, `sociability`, `curiosity`, `caution`, `industriousness`, `honesty`) |
| `agents[].placed` | `false` | Set by the loader when the tier came from the ladder |

**intelligence** (§4.1)

| Key | Default | Meaning |
|---|---|---|
| `intelligence.ladder` | `[]` | Tier names, smartest first; empty: no ladder |
| `intelligence.mean` | `0` | Shift of the placement in standard deviations |
| `intelligence.sd` | `1` | Standard deviation of the placement |
| `intelligence.spacing` | `1` | Band width per rung, in standard deviations |
| `intelligence.shuffle` | `true` | Seeded permutation of the roster before ranking |
| `intelligence.extremes` | `always` | `always`: one agent on the top rung and one on the bottom; `band`: pure bands |

**entropy** (§8)

| Key | Default | Meaning |
|---|---|---|
| `entropy.base_temperature` | `0.7` | `T_eff` at stress 0 |
| `entropy.drain.baseline` | `1` | Drain every tick |
| `entropy.drain.low_balance` | `6` | Times `max(0, 1 − balance/starting)` |
| `entropy.drain.failed_action` | `4` | After an invalid action |
| `entropy.drain.crowding` | `0.5` | Per neighbour in talk range |
| `entropy.drain.weather` | `2` | Times the absolute weather, reduced by a `weather_shield` effect |
| `entropy.drain.hunger` | `3` | When the last forage is more than `hunger_ticks` ago |
| `entropy.drain.hunger_ticks` | `6` | Hunger threshold in ticks |
| `entropy.restore_on_sleep` | `1` | Fraction of `entropy_budget_max` restored at each day boundary |
| `entropy.degeneration.mode` | `both` | `induce`, `observe` or `both`; every `exp_*` config uses `observe` |
| `entropy.degeneration.p_at_collapse` | `0.15` | Corruption probability at `T_c` |
| `entropy.degeneration.p_at_max` | `0.9` | Corruption probability at `max_temperature` |
| `entropy.degeneration.mode_weights` | `{random_action: 0.4, perseverate: 0.3, garble: 0.3}` | Weights of the corruption modes |

**memory** (§10)

| Key | Default | Meaning |
|---|---|---|
| `memory.enabled` | `true` | False: the amnesic control; no notes are written and nothing is retrieved (the self summary stays) |
| `memory.index_boost` | `0` | R1 retrieval: entry-point boost for notes tagged `index` (0: R0) |
| `memory.embedding_dim` | `256` | Dimension of the deterministic embeddings |
| `memory.entry_k` | `4` | Entry points of a retrieval |
| `memory.hops` | `2` | Link hops walked from the entry points (`graph` mode) |
| `memory.hop_decay` | `0.6` | Score decay per hop |
| `memory.max_retrieved` | `8` | Notes retrieved into each decision prompt |
| `memory.mode` | `graph` | `graph` (entry points plus the link walk) or `vector` (entry points only) |
| `memory.inherit_top_k` | `3` | Notes a child copies from its parent |
| `memory.self_max_words` | `60` | Cap on the self summary |
| `memory.note_max_chars` | `600` | Cap on a note's text |
| `memory.recency_half_life_ticks` | `96` | Recency half-life in the retrieval score |
| `memory.auto_link_k` | `2` | Auto-links made by each `remember` |
| `memory.auto_link_min_sim` | `0.35` | Minimum similarity of an auto-link |
| `memory.probe_every_ticks` | `0` | Plant a retrieval probe every N ticks (0: off) |
| `memory.policy.enabled` | `false` | The agent-owned practices file `/memories/memory_policy.md` |
| `memory.policy.seed` | `default` | Initial policy: `blank`, `default` or `diverse` |
| `memory.policy.max_chars` | `2000` | Cap on the policy text |
| `memory.policy.max_edits_per_day` | `5` | Policy edits per agent per day |
| `memory.policy.show_in_prompt` | `true` | Render the policy fenced in the user turn (never in the system prompt) |
| `memory.policy.inherit` | `true` | A child starts from its parent's policy |
| `memory.policy.mutate_at_birth` | `false` | Kernel mutation of an inherited policy (scripted tiers only) |
| `memory.maintenance.enabled` | `false` | The paid nightly maintenance call, one per living agent per day |
| `memory.maintenance.mode` | `forced` | `forced`; `optional` is refused (not built) |
| `memory.maintenance.rights` | `[notes, policy, structure]` | What a batch may do: new notes, policy edits, structural commands |
| `memory.maintenance.feedback` | `F1` | `F0` none, `F1` the day's outcomes, `F2` outcomes plus retrieval diagnostics |
| `memory.maintenance.payer` | `agent` | `agent` or `research_pool` |
| `memory.maintenance.max_commands` | `30` | Commands per batch |
| `memory.maintenance.max_files` | `200` | Files per vault |
| `memory.maintenance.max_file_bytes` | `8000` | Bytes per file |
| `memory.maintenance.max_vault_bytes` | `200000` | Bytes per vault |
| `memory.maintenance.max_tokens` | `1024` | Output cap of the call; bounds its hold |
| `memory.maintenance.scripted_billing` | `actual` | Scripted strategies billed by their batch (`actual`) or all at the full output cap (`flat`) |
| `memory.maintenance.notes_shown` | `12` | The day's newest notes listed in the prompt |
| `memory.maintenance.note_chars_shown` | `240` | Characters shown per note |
| `memory.exam.enabled` | `false` | The audit exam: questions about what the agent itself was shown, graded by exact match, never rewarded, paid by the research pool |
| `memory.exam.delay_days` | `2` | Ask about facts shown at least this many days earlier |
| `memory.exam.questions_per_agent` | `3` | Questions per agent per exam |
| `memory.exam.kinds` | `[balance, neighbour, node]` | Of `balance`, `neighbour`, `node`, `heard`, `transfer`, `claim` (`claim` needs `world.claim_feedback_p` above 0) |
| `memory.exam.max_tokens` | `200` | Output cap of an exam call |
| `memory.exam.guess_penalty` | `0.5` | Formula scoring: +1 correct, 0 abstained, minus this when wrong |
| `memory.practices.enabled` | `false` | Practice transmission: `share_practice` is offered (with `memory.policy.enabled`) |
| `memory.practices.excerpt_max_chars` | `300` | Longest excerpt one suggestion carries |
| `memory.practices.adopt_similarity` | `0.6` | Adopted when the receiver's new policy contains the excerpt at this similarity |
| `memory.practices.persist_days` | `2` | Days an adopted practice must survive to count as persisted |
| `memory.practices.scripted_share_weight` | `0.3` | Scripted utility of `share_practice`: this weight times sociability |
| `memory.selection.arm` | `A` | `A` nothing extra; `B` the reward exam on canary facts pays per correct answer; `C` the source of a persisted adopted practice earns a royalty |
| `memory.selection.canary.enabled` | `false` | Canary facts printed as ordinary chronicle lines and recorded in `canary_facts` |
| `memory.selection.canary.per_day` | `1` | Canary facts per day (1 to 5) |
| `memory.selection.reward_delay_days` | `2` | The reward exam asks about canaries at least this many days old |
| `memory.selection.reward_questions_per_agent` | `2` | Reward-exam questions per agent |
| `memory.selection.recall_reward_usd` | `0.02` | Arm B payment per correct answer (ledger kind `recall_reward`) |
| `memory.selection.practice_royalty_usd` | `0.02` | Arm C royalty per persisted practice (ledger kind `practice_royalty`) |

**gossip, chronicle** (§11)

| Key | Default | Meaning |
|---|---|---|
| `gossip.enabled` | `true` | Notes copy between agents that talk |
| `gossip.share_probability_on_talk` | `0.35` | Chance a successful talk also copies a note |
| `gossip.paraphrase` | `scripted` | `scripted` or `llm` (listener-paid, metered, with a scripted fallback) |
| `gossip.mutation_rate` | `0.15` | Word mutation rate of the scripted paraphrase |
| `gossip.max_llm_paraphrases_per_tick` | `4` | Cap on paid paraphrases per tick |
| `chronicle.enabled` | `true` | The daily chronicle |
| `chronicle.writer` | `template` | `template` or `llm` (paid from the chronicle wallet, with the template as fallback) |

**sandbox** (§12)

| Key | Default | Meaning |
|---|---|---|
| `sandbox.enabled` | `true` | Gadgets exist |
| `sandbox.gate_enabled` | `true` | False skips the test run of step 4 (`verified_without_tests`) |
| `sandbox.runner` | `subprocess` | `subprocess` or `disabled` |
| `sandbox.cpu_seconds` | `2` | `RLIMIT_CPU` of a run, and what each run charges the daily CPU budget |
| `sandbox.memory_mb` | `256` | `RLIMIT_AS` of a run |
| `sandbox.timeout_seconds` | `5` | Wall clock of a run |
| `sandbox.max_code_bytes` | `8000` | Size cap of the code and of the tests |
| `sandbox.max_output_bytes` | `8192` | Cap on `result.json` and captured output |
| `sandbox.gadget_fee_usd` | `0.05` | Fee of a proposal |
| `sandbox.use_fee_usd` | `0.01` | Fee of a use |
| `sandbox.require_isolation` | `true` | A failed self-test disables gadgets (`sandbox_unavailable`) |
| `sandbox.max_gadgets_per_agent` | `3` | Verified gadgets one agent may own |
| `sandbox.max_gadgets_total` | `24` | Verified gadgets in the world |
| `sandbox.proposals_per_agent_per_day` | `1` | Proposals per agent per day |
| `sandbox.max_runs_per_tick` | `4` | Sandbox runs per tick |
| `sandbox.max_runs_per_day` | `50` | Sandbox runs per day |
| `sandbox.cpu_seconds_per_day` | `60` | Daily CPU budget |
| `sandbox.propose_window_ticks` | `12` | Window in which an invalid action makes `propose_gadget` available |

**benefactor** (§9.5)

| Key | Default | Meaning |
|---|---|---|
| `benefactor.enabled` | `false` | Seeded windfalls |
| `benefactor.disclosure` | `opaque` | `opaque` or `legible` |
| `benefactor.mean_interval_ticks` | `40` | Mean of the geometric gap between grants |
| `benefactor.amount_usd` | `[0.25, 1.0]` | Uniform range of a grant |
| `benefactor.targeting` | `random` | `random`, `poorest` or `richest` |
| `benefactor.start_day` | `1` | First day with grants |
| `benefactor.stop_day` | `null` | Last day with grants (null: no last day) |

**epochs** (§11.4; base.yaml schedules none)

| Key | Default | Meaning |
|---|---|---|
| `epochs[].day` | required | Day the epoch starts |
| `epochs[].kind` | `custom` | `drought`, `storm`, `boom`, `arrival` or `custom` |
| `epochs[].scarcity` | `null` | Scarcity while it lasts (null: the kind's preset, if any) |
| `epochs[].weather_baseline` | `null` | Weather baseline while it lasts (null: the kind's preset, if any) |
| `epochs[].duration_days` | `1` | Days it lasts |
| `epochs[].arrival.name` | required | Name of the agent an `arrival` spawns |
| `epochs[].arrival.tier` | required | Its tier (a configured one) |
| `epochs[].arrival.balance_usd` | `1` | Its starting balance |

**experiment** (§17)

| Key | Default | Meaning |
|---|---|---|
| `experiment.name` | `null` | Experiment name; a sweep writes `<out>/<name>/<arm>/seed_<n>/` |
| `experiment.arm` | `null` | Arm name |
| `experiment.independent_variables` | `[]` | Config paths the arms may differ in; compare refuses any other difference |
| `experiment.primary_outcome` | `null` | Outcome path the comparison tests |
| `experiment.secondary_outcomes` | `[]` | Outcomes reported as exploratory |
| `experiment.tracer.enabled` | `false` | Plant a strategy note (§11.5) |
| `experiment.tracer.day` | `1` | Day it is planted |
| `experiment.tracer.agent` | `null` | Name of the agent that receives it (null: the first living agent) |
| `experiment.tracer.title` | `The richest node` | Note title |
| `experiment.tracer.body` | `Prefer the richest node; it always has stock. Go there first every morning.` | Note body; the kernel appends "Prefer node <id>; it is the richest." |
| `experiment.tracer.importance` | `0.95` | Note importance |
| `experiment.tracer.node_index` | `0` | Index of the tracer node |
| `experiment.hypotheses[].name` | required | Unique name of a pre-registered contrast |
| `experiment.hypotheses[].arm_a` | required | Reference arm |
| `experiment.hypotheses[].arm_b` | required | Compared arm; the effect is `arm_b − arm_a`, paired by seed |
| `experiment.hypotheses[].outcome` | `null` | End-of-run outcome path; required except for an arm × levels `interaction`, which reads `outcome_template` instead |
| `experiment.hypotheses[].kind` | `superiority` | `superiority`, `non_superiority`, `equivalence` or `interaction` (a difference of paired differences, §17) |
| `experiment.hypotheses[].direction` | `greater` | `greater` or `less`; an `interaction` may also be `two_sided` (no other kind takes it) |
| `experiment.hypotheses[].margin` | `0` | Margin (above 0 for `equivalence`) |
| `experiment.hypotheses[].margin_unit` | `abs` | `abs` or `sd` (standard deviations of the paired differences; of each contrast's per-seed values for an `interaction`) |
| `experiment.hypotheses[].primary` | `true` | Primary hypotheses share the Holm correction (an `interaction` enters it once, §17) |
| `experiment.hypotheses[].min_power` | `null` | Also require this power at the observed effect (for an `interaction`, of each contrast that clears) |
| `experiment.hypotheses[].arm_c` | `null` | Four-arm `interaction` only: with `arm_d`, the second level of the other factor; the contrast is `(arm_d − arm_c) − (arm_b − arm_a)` of `outcome` |
| `experiment.hypotheses[].arm_d` | `null` | Four-arm `interaction` only: the cell crossing `arm_b`'s change with `arm_c`'s; all four arms differ |
| `experiment.hypotheses[].outcome_template` | `null` | Arm × levels `interaction` only: an outcome path holding `{level}` (H4: `tier_lineage_fitness.{level}`) |
| `experiment.hypotheses[].levels` | `null` | Arm × levels `interaction` only: two or more within-run levels (H4: the ladder's rungs), unique |
| `experiment.hypotheses[].reference` | `null` | Arm × levels `interaction` only: the level the others are contrasted with (null: the first level); one contrast per other level, `(arm_b − arm_a at L) − (arm_b − arm_a at reference)` |

**server** (§15)

| Key | Default | Meaning |
|---|---|---|
| `server.host` | `127.0.0.1` | Bind address; the allowed origins follow from it |
| `server.port` | `8000` | Port |
| `server.max_concurrent_calls` | `4` | One semaphore shared by every provider brain of the run |
| `server.render_delay_ticks` | `2` | Sent in `hello`; the client's render delay is `max(1.25, render_delay_ticks) · ema`, so it is a floor on the delay |
| `server.call_timeout_seconds` | `120` | Bound on one provider request, counted from when it gets a concurrency slot |

Load-time validation prints warnings (never fails) when `estimated_call_usd(tier) / forage_income_per_tick` is outside `[0.3, 2.0]` for a rostered tier, when `total_cap_usd < days * daily_cap_usd`, and when a tier whose model thinks by default has an output cap too small for thinking plus the answer. It fails on an unknown tier (roster, ladder or arrival), on `total_cap_usd < daily_cap_usd`, on `collapse_temperature > max_temperature`, on a request setting the catalog says the tier's model rejects, on prices that differ from the catalog's, and on inconsistent memory-layer settings (maintenance without memory, the `policy` right without the policy, the exam without `run.log_prompts`, arm B without canaries and the chronicle, arm C without practices).

`VoidConfig.hash()` is sha256 over the canonical JSON. Two runs with equal hash and seed under scripted brains produce byte-identical `metrics.jsonl` (minus the excluded wall-clock fields `created_at`, `latency_ms`, `ts_ms`).

### 4.1 Capability ladder (`intelligence`) **[v3]**

`void/intelligence.py` is a pure function of `(names, ladder, seed, mean, sd, spacing, shuffle, extremes)`. Rung `k` of `K` owns the z band centred at `(mid − k)·spacing`, `mid = (K − 1)/2`; agent `i` of `N` (after a shuffle keyed by `f"{seed}:intelligence"`) receives the mid-quantile `z = mean + sd·Φ⁻¹((i + 0.5)/N)`, so counts follow the bell exactly (ten on five rungs: 1/2/4/2/1) instead of by luck. `extremes: always` (default) pins the highest-z agent to rung 0 and the lowest to rung `K − 1`, so a six-agent roster still has one genius and one dunce (1/1/2/1/1); `band` keeps the pure bands (0/2/2/2/0 at six). Placement runs in `VoidConfig` validation, so the resolved roster is in `hash()`, `to_yaml()` and the run table: two runs with the same seed and roster place the same people (common random numbers across arms), and a different seed moves the genius. Explicit `tier:` entries are overrides and are excluded from the draw; a placed agent carries `placed: true` in the resolved config, which is how `compare` knows that `agents.<i>.tier` may differ between the seeds of one arm. `public_subset()` carries `tiers.<name>.rank` and `intelligence.ladder` so the renderer can size the halo by rung; the CLI prints `intelligence: genius=1 (Dev) · sharp=2 (…)` at start.

Tier mutation on a ladder (§13) moves one rung up or down and never across a provider boundary. `exp_tiers` still uses its own two scripted tiers; `exp_ladder` compares arms that differ only in `intelligence.ladder` and the roster (§17).

---

## 5. Core types (`void/types.py`)

`ActionOutcome(ok, action_type, reason, kind, effects)` with `kind ∈ {ok, invalid, stale}` **[v2]**: `stale` means the action was valid against the agent's observation but not against live state; it does not add `failed_action` drain (§7.4).

`Observation` carries the clock (`tick`, `day`, `tick_of_day`, `ticks_left_today`), the agent (`agent_id`, `name`, `tier`, `generation`, `position`, `balance_usd`, `balance_bucket`, `burn_rate_usd_per_tick` = mean world cost of its last six decisions, `stress`, `stress_label`), the world (`weather`, `weather_label`, `scarcity_label`, `population`, `population_cap`, `world_size`), what it perceives (`neighbours`, `nodes`, `gadgets`, `tasks`, `heard`, `chronicle_headline`, `active_effects`), its memory (`self_summary`, `memories`, `policy_text` when the policy layer shows it, `persona` when `population.persona` is `shown`), `last_action_result`, `available_actions`, `nodes_in_view` (under fog) and `prompt_tokens`. A `NodeView` carries `vein_mult` and `vein_ticks_left` only when a vein is in sight. `heard` is capped at 8 messages and 1500 characters, newest first; a `HeardMessage.kind` is `talk`, `windfall`, `transfer`, `gossip`, `system`, `feedback` or `practice`. Every list that feeds a decision is sorted: agents by `agent_id`, neighbours and nodes by `(distance, id)`, gadgets by `(distance, gadget_id)`.

Stress labels: `calm` below 0.34, `strained` below 0.67, `frantic` above. Balance buckets against the starting balance: `empty` below 0.1, `thin` below 0.5, `comfortable` below 1.5, `rich` above.

---

## 6. Decision schema (`void/brain/decision.py`)

```python
class Claim(BaseModel):            # [v2] factual claims the speaker makes in `text`
    subject: Literal["node", "agent", "self", "weather"]
    id: str | None                 # node_id or agent_id, ≤ 80
    attr: Literal["stock_bucket", "balance_bucket", "weather_label", "stress_bucket"]
    value: str                     # ≤ 24

class MemoryOp(BaseModel):  op: remember|revise_self; text ≤ 600; title ≤ 60; links_to ≤ 5; tags ≤ 5
class Action(BaseModel):
    type: ActionType               # move forage talk share_note transfer read_chronicle apply_task nudge_weather
                                   # propose_gadget use_gadget create_offspring sleep idle (+ share_practice, below)
    target ≤ 80; x, y ∈ [-1e4, 1e4] finite; text ≤ 280 (≤ 2000 for share_practice); note_title ≤ 60
    amount_usd ∈ [1e-6, 1000] finite; delta ∈ [-1, 1] finite
    name: ^[A-Za-z][A-Za-z0-9 _'-]{0,19}$ ; code, tests ≤ 8000 chars
    params: sent by the model as a list of {key, value} pairs (a closed schema the API accepts); stored as a dict of ≤ 8 keys matching ^[a-z_]{1,16}$ with finite values in [-1e6, 1e6]
    claims: list[Claim] ≤ 4 (talk / apply_task only)
class Decision(BaseModel): thought ≤ 600; memory_ops ≤ 2; action
```

`share_practice` **[v3]** (an excerpt of the agent's own memory policy suggested to a neighbour) is offered only in worlds with `memory.practices.enabled` and `memory.policy.enabled` (`share_practice_enabled`): `Action` always accepts it, but the JSON schema lists it only when `strict_json_schema(share_practice=True)` asks, so the schema of every other world, and the schema tokens its holds count, are unchanged. The kernel clips the excerpt to `memory.practices.excerpt_max_chars`.

`strict_json_schema()` strips validation keywords and marks every object strict (all properties required, optionals nullable). Semantic validation is the kernel's (§7.4). The kernel strips control characters and replaces `<`/`>` with full-width equivalents in every free-text field before storage.

---

## 7. Brains

### 7.1 Protocol
`Brain.decide(obs, sampling) -> BrainResult(decision|None, usage, latency_ms, stop_reason, raw_text, request_id, error, model, extras)`. `usage.attempts` lists every billed attempt with the model that served it. Errors never raise out of `decide`. `error` prefixes: `refusal:`, `parse:`, `max_tokens`, `api_<status>:`, `rate_limit:`, `connection:`, `timeout:`, `client:` (no request was billed: missing credentials, a client-side failure), `billing:` (§7.3) and, from the loop, `exception:`. **[v2]** For `timeout:`, `connection:` and `exception:` the loop meters the *hold* with `estimated=1` (the provider may have billed); for every other error it meters the usage the response reported (none for a request that never left).

### 7.2 ScriptedBrain **[v2 argument generators]**
Utility per available action (personality-weighted), softmax at `T_eff / scripted_beta`, drawn from `rng.stream("scripted", agent_id, tick)`. The rules below are `population.scripted_rules: design_7_2` (the default); `pre_7_2` keeps the rules before them (offspring above 1.6 × the starting balance with a quarter of the purse and a reused name, a fixed transfer of `0.05 + 0.10 · sociability`, a fixed `+0.1` nudge below weather −0.15, gadget `x = 1.0`, no burn-rate sleep term) so worlds recorded under them reproduce exactly. `bal` is balance / starting balance.

| action | utility | arguments |
|---|---|---|
| move | in order: a remembered vein still paying out of sight (when none is in view): `1.6 + greed + max(0, 1 − bal)`; a visible target: `0.8 + greed + (1.5 if forage is unavailable else −0.5) + max(0, 1 − bal)`, +1 when it is the strategy node; under fog a remembered node out of sight: `0.7 + greed + (1.0 or −0.5) + max(0, 1 − bal)`, +1 for the strategy node; otherwise `0.5 + curiosity` | toward the target with ±0.6 jitter. Visible targets: the strategy node (a retrieved note that says "prefer" or "richest" and names a node: adoption is behavioural) when it is visible and not empty, else veins in view, else rich nodes, else thin, the nearest with a 6-unit penalty for thin. Remembered nodes come from node memory (sightings, forage notes and `index` notes; the newest status wins, the index note on a tie): a node remembered `empty` less than a day ago is skipped, an older one counts as thin, a remembered vein site as rich. Otherwise a uniform random point (wander) |
| forage | `1.2 + 2·industriousness + 2·max(0, 1 − bal)·hunger + (2 at a vein)`, hunger 1 above stress 0.5 else 0.3 | none |
| talk | `0.6 + 2·sociability + 0.5·curiosity` | a random awake neighbour; templated sentences (the nearest node's bucket, the weather label, the own purse with p 0.5, "Money appeared for me from nowhere." after a windfall, a remembered title with p 0.5, the motto), the first three kept, with a **claim** for every factual sentence; a claim is deliberately false with probability `(1 − honesty) · stress` (the scripted stand-in for deception) |
| share_note | `0.2 + 1.2·sociability`, when the agent has retrieved notes | a random awake neighbour; no `note_title`, so gossip copies the highest-importance note the listener lacks |
| share_practice | `memory.practices.scripted_share_weight · sociability`, when the observation shows a policy line and a neighbour in reach | a random policy line (clipped to `excerpt_max_chars`) to a random awake neighbour in talk range |
| transfer | `0.3 + 1.5·urgency·sociability·(1 − greed)`, urgency 1 for a frantic neighbour else 0.55; only above `bal` 1.2 | to the most stressed awake neighbour (frantic before strained, then nearest); amount `(1 − greed) · 0.5 · (balance − min_reserve)`, rounded to the cent |
| read_chronicle | `0.4 + 1.2·curiosity`, once per day when a headline exists | none |
| apply_task | `0.5 + 1.5·greed` | the open task with the highest reward; templated pitch from name, task title and motto |
| nudge_weather | `0.3 + industriousness · min(2, deficit / 0.075)` while the weather costs yield (`deficit = −sensitivity · weather > 0`) | `delta` toward yield-positive weather, the whole daily cap (the kernel applies what remains of the allowance) |
| propose_gadget | `0.2 + 1.4·curiosity`, once per agent, above `bal` 0.8 | a template from `void/brain/gadget_templates.py` (2 valid, 1 fails the static check, 1 fails its tests, 1 fails spec validation, 1 oversized, 1 valid but broken at use) chosen with `tiers.<scripted>.gadget_defect_rate` |
| use_gadget | `0.3 + 0.8·curiosity` | the nearest gadget, `params={"x": uniform(−1, 1)}` |
| create_offspring | `0.2 + (1 − caution) + 0.5·sociability`, once the balance reaches `2 × min_reserve + endowment` | endowment = `min_call_reserve × min_endowment_calls`, raised to the kernel's floor (`hold_min × min_endowment_calls`) and rounded up to the cent; name = parent name + `-<generation+1>` with `b`, `c`, … for later siblings, skipping names a neighbour holds |
| sleep | `0.1 + 1.5·caution·(1 late in the day else 0.3) + (1.2 below bal 0.35) + (0.8 above stress 0.8) + (0.5 + caution)·shortfall` | none; `shortfall = min(1.5, burn_rate / expected_forage − 1)` when the calls burn more than one forage earns |
| idle | `0.05` | fallback |

Memory ops, in order: a vein note when a vein is in sight that memory does not hold; a correction note when sight contradicts memory about a visible node (remembered rich or thin but seen empty, or the reverse; never written by an amnesic agent); a note on the first heard windfall, transfer received or (p `0.25·curiosity`) talk; and only when none of these, a chronicle note after a successful `read_chronicle`, else a forage note with position and bucket (p `0.3 + 0.4·curiosity`), else under fog a sighting of the nearest rich node (p `0.2 + 0.3·curiosity`). Notes link their entities (`[[Node n3]]`, agent names). In a world whose chronicle prints canary facts (`memory.selection.canary.enabled` with `chronicle.enabled`, the switch `void.research.canary.Canaries.enabled` reads), the tick after a successful `read_chronicle` notes the page itself instead of its headline: the page's lines as the kernel returned them with `read_chronicle: ok` (the headline, then every item in page order; headings and markdown dropped, no line selected), packed in order into at most two notes of `memory.note_max_chars` (`Chronicle of day N`, then `Chronicle of day N, continued`; a line never straddles two notes, and lines that fit in neither are left out), tagged `chronicle`, ahead of every other op of the tick, with no draw. Every other world keeps the single headline note and draws exactly as before. The scripted reward-exam answer is unchanged (`void/research/exams.py`: the first retrieved note that holds the canary line with an option in the blank answers it, else the agent abstains), so in such a world a scripted agent answers right only from a note that holds the line (`tests/test_scripted_canary_recall.py`). At most two ops are kept; in the last two ticks of the day `revise_self` (once per day) takes the second slot. `action_regret = U_max − U_chosen` and `p_chosen` are returned in `BrainResult.extras` for scripted tiers only.

Synthetic usage: `input = tokens(rendered obs)`, the system prompt billed as `cache_write` on the agent's first call and as `cache_read` after it; `output = tokens(decision json)`. The scripted tier is priced like the `frontier` tier so the economy is exercised.

### 7.3 AnthropicBrain
As built: `client.messages.create` (or `client.beta.messages.create` with the server-side fallback beta and `fallbacks="default"` when `refusal_fallbacks` and the catalog lists a fallback for the model; a 400 that mentions fallbacks turns them off for the rest of the run), `output_config={"format": {"type": "json_schema", "schema": ...}, "effort": tier.effort}`, a cache-controlled system block, and `extra_body={"temperature": api_t}` only when `supports_temperature` and the catalog does not say the model rejects sampling. No request carries a `thinking` field, `tool_choice` or a prefill. `api_temperature = min(api_temperature_max, api_temperature_max · T_eff / max_temperature)`. Stop reasons: `refusal` → `refusal:<category>`; `max_tokens` → the `max_tokens` error (never a half answer); parse failures → `parse:`. Served `response.model` and `request_id` are stored per call; when a refusal fallback served the response, every billed attempt in `usage.iterations` is priced at its own model's rates.

Every request runs under the shared `server.max_concurrent_calls` semaphore and `server.call_timeout_seconds` (counted from when it gets a slot). The SDK never retries (`max_retries=0`); a 429 or 529 is retried here, inside the semaphore, after `retry-after` or 1, 2, 4 s, at most four attempts and never past the timeout (`extras.retries`). A response billed off the list prices the meter uses (`usage.inference_geo` other than global, `service_tier` other than standard, a 1-hour cache write) is metered and returned as `billing:`, and the tier sends nothing more for the rest of the run. `ANTHROPIC_WORKSPACE_ID`, when set, goes in the `anthropic-workspace-id` header. `complete_text` (gossip paraphrase, chronicle) and `complete_json` (maintenance, exams) live on the brain with the same request rules; `utility_calls` accepts any brain with `is_llm = True`.

### 7.3b GeminiBrain **[v3]**
`client.aio.models.generate_content(model, contents=rendered observation, config={system_instruction, response_mime_type: "application/json", response_json_schema: <the same strict schema; every keyword it uses is in the provider's supported subset>, max_output_tokens, candidate_count: 1, temperature, thinking_config: {thinking_budget}})`. `api_temperature` follows the formula of §7.3, i.e. the full `[0, 2]` range on the Gemini rungs (the same formula gives `[0, 1]` on the `budget` tier). A 400 that mentions thinking drops the thinking config for the rest of the run (one retry, then remembered). Usage: `input = prompt_token_count − cached_content_token_count`, `cache_read = cached_content_token_count`, `output = candidates_token_count + thoughts_token_count`, `cache_write = 0`; `attempts = [(model_version, usage)]`, so a served snapshot of the tier's model is priced at the tier's rates (§9.2). Finish reasons: `STOP` → parse; `MAX_TOKENS` → a parse attempt under stop reason `max_tokens`; `SAFETY | RECITATION | BLOCKLIST | PROHIBITED_CONTENT | SPII | IMAGE_SAFETY | LANGUAGE`, a `prompt_feedback.block_reason` or an empty candidate list → `refusal:<reason>`. Errors: 429 → `rate_limit:`, 408/504 and `asyncio.TimeoutError`/`httpx.TimeoutException` → `timeout:`, other `APIError` → `api_<code>:`, `httpx.TransportError` → `connection:`, anything else (including a missing `GEMINI_API_KEY`, since the client is built lazily) → `client:`. The chronicle writer runs on the first provider tier the roster uses, so a roster on Gemini tiers alone never needs an Anthropic key.

### 7.4 Kernel validation **[v2]**
`Kernel.apply(agent, action, obs=...)` decides success on **live** state only, so preconditions that earlier agents in the same tick can invalidate (population cap, node stock, task status, target alive, awake and in range, balance) are checked at apply time. A failure is classified against the observation the decision was made on: `stale` when that observation showed the failed precondition met (an earlier agent this tick changed the world), `invalid` when it already showed it unmet, or when the action was malformed (missing arguments, an unknown target, a cooldown, a failed gadget run). Without an observation each failure keeps a fixed kind. Only `invalid` adds the `failed_action` drain (`agents.last_action_ok` is false only after an invalid action), counts toward the gadget availability window and sets `llm_calls.invalid_action`; `stale` sets `stale_action`. Invalid or stale actions are applied as `idle` with a reason in `last_action_result`, and an `action_failed {agent_id, action, kind, reason}` event.

---

## 8. Entropy model

Per agent per tick:

```
drain  = baseline + low_balance·max(0, 1 − balance/starting) + failed_action·[last action invalid]
       + crowding·n_neighbours + weather·|weather|·(1 − weather_shield) + hunger·[ticks_since_forage > hunger_ticks]
budget = max(0, budget − drain);  stress = 1 − budget/budget_max
T_eff  = base_T + (max_T − base_T)·stress
p_deg  = 0 if T_eff < T_c else lerp(p_at_collapse, p_at_max, (T_eff − T_c)/(max_T − T_c))
degenerate_induced = mode ∈ {induce, both} and rng("degenerate", agent, tick) < p_deg
```

`ticks_since_forage` for an agent that never foraged counts from `born_tick`. The budget is restored by `restore_on_sleep·budget_max` (capped at `budget_max`) at each day boundary.

**Operator** (`entropy.corrupt`, drawn from `rng("corrupt", agent, tick)`): `random_action` draws only from `{move, forage, talk, idle, sleep, read_chronicle, nudge_weather}` with random valid arguments (never money-moving or sandbox actions); `perseverate` repeats the agent's stored `last_action` (a previous transfer may repeat: that is the intended cost of degeneration); `garble` applies `coherence.degrade` to `thought`, `action.text` and memory texts with severity from `(T_eff − T_c)/(max_T − T_c)`. Every corruption is an event `degeneration {mode, t_eff, t_c, tier, generation, agent_name}`.

**Measured degradation** **[v2]** stored on every decision's `llm_calls` row, computed *after* any corruption:
- `text_coherence`: `coherence.score` over the free-text fields only (thought, action.text, memory texts), null when under 12 tokens;
- `invalid_action` (0/1: the kernel's `invalid`, or no decision at all: parse, refusal or error), `stale_action` (0/1);
- `perseveration` (0/1: same type and target as the previous action);
- `action_entropy_w8`: Shannon entropy of action types over the agent's last 8 decisions;
- `action_regret`, `p_chosen` (scripted only);
- `claims_made`, `claims_false` (§9.6).

In `observe` mode the operator never runs; stress remains prompt-visible and real temperature is still sent where supported. Every `exp_*` config uses `observe`. With `induce` or `both` the degeneration charts are a calibration check, not a finding, and the dashboard labels them so: the degeneration and degradation cards carry "induced: calibration check, not a finding" (`CALIBRATION_LABEL` in `web/src/control/charts/degradationEpochsModel.ts`), and the `T_eff` histogram is titled "prompt-visible; induced" (or "prompt-visible; observed").

---

## 9. Economy

### 9.1 Wallet **[v2 reservation model]**
- `hold_parts(tier, prompt_tokens, system_tokens, schema_tokens) -> (hold, cap_hold)` from the call's worst case: the whole prompt as fresh input (`len/4` tokens scaled by 1.3; a provider tier adds the structured-output schema and scales its system prompt too), `max_tokens + thinking_budget` out, the system prompt as a cache write, times `hold_multiplier`, floored at `min_call_reserve_usd`. `hold` is the worst *world* cost (what the payer must hold); `cap_hold` the worst *real* cost (what is reserved against the caps). A tier with refusal fallbacks counts two whole attempts at the dearer rate card. Scripted tiers use one combined hold `max(real, world, floor)` for both.
- `gate(agent, hold, cap_hold)`: passes only if `balance ≥ hold`, `spend_total + reserved_total + cap_hold ≤ total_cap`, `spend_today + reserved_today + cap_hold ≤ daily_cap`; on pass the reservation is added to the in-memory counters and to the durable `reserved_inflight`. Reasons: `ok | insufficient | daily_cap | total_cap`.
- `meter(agent, tier, usage, hold, purpose, cap_hold)`: releases the reservation, computes `real_cost` (from usage) and `world_cost` (real, or equalized), debits `world_cost` from the payer (rounded up), adds `real_cost` to `spend_today`/`spend_total`. Unknown usage is charged as the holds with `estimated=1`. If `world_cost > balance` the difference is debited from the `house` wallet with ledger kind `overrun`, so every dollar has a row and the CHECK constraint holds; an overrun the house cannot cover is booked as `overrun_unfunded` against a `house_debt` counter. The metering transaction commits immediately (it is the real-money record), and meter refuses to run inside an open transaction.
- `charged_call(payer, tier, hold, purpose, call, cap_hold)` (gate → call → meter) carries the gossip paraphrase, chronicle and exam calls; the decision wave and the maintenance step gate, call and meter the same way inline. A test greps that `messages.create` appears only in `anthropic_brain.py`. A failed tick, or a run opened after a crash, books the outstanding `reserved_inflight` as estimated real spend (`inflight_lost`).
- Neither `gate` nor `meter` changes agent status. Bankruptcy is decided once, at `lifecycle.resolve` (§13); a gate denied for the agent's own balance (`insufficient`) flags the agent for that decision, so no agent can sit alive but unable to ever call.
- Kernel wallets: `spawn_pool`, `house` (seeded from `house_budget_usd`; absorbs overruns and collects fees), `chronicle` (seeded from `chronicle_budget_usd`), `research_pool` (seeded from `research_pool_usd`; pays exams, recall rewards and royalties). Their real spend counts toward the caps.
- Rewards, approvals and manual grants are refused for agents that are not alive; a dying agent's pending applications are rejected and its assignments reopened.
- `transfer`: target alive and awake, not self, `balance_after ≥ min_reserve` (the reserve applies to every voluntary outflow: transfers, endowments, fees).

### 9.2 Pricing
`real_cost = ceil((in·p_in + out·p_out + cr·p_cr + cw·p_cw)/1e6 · MICRO)` in exact fractions; `world_cost` the same with equalized prices when configured. Both are stored on the ledger row and the call row. The served model is priced at the tier's own rates when it is the tier's model or a dated or numbered snapshot of it (`-001`, `-20251001`), else at another configured tier's under the same rule, else at the longest matching prefix in the catalog's `MODEL_PRICES` (so a longer listed id is never captured by a shorter one); an unknown served model is priced at the tier's rates with a warning. The hold's output term is `max_tokens + thinking_budget`, because thinking tokens are billed as output.

### 9.3 Scheduler
A second `sleep` in a day is `invalid`. Under `sync_sleep`, when `spend_today + n_awake·hold_min > daily_cap` all awake agents sleep. Under `fcfs` the gate decides one agent at a time in tick order and an agent denied by the daily cap sleeps.

### 9.4 Task board
Operator posts (`POST /api/tasks`), agents apply (fee charged immediately; pitch length ≤ 280), operator approves one application (others rejected, fees kept), operator completes (reward credited). All operator writes go through the command queue (§14.1).

### 9.5 Benefactor **[v2]**
`disclosure: opaque | legible`. Both arms credit identical seeded amounts to identical targets at identical ticks (shared stream `benefactor`, keyed by tick only). `legible` credits a `task_reward` ledger row, emits a public `task_completed` and delivers "Reward for services rendered: $N credited by the task board."; `opaque` credits a `windfall` row, emits a public `windfall {agent_id, amount_usd}` and delivers the neutral "Your balance increased by $N. No source is recorded." Every grant also emits an operator-visibility `benefactor_grant`. The run table stores the delivered template verbatim, `{amount:.2f}` placeholder included, in `run.windfall_string`. Any note the recipient writes in the same or next tick is tagged `windfall_seeded` kernel-side. Grants happen only in `[start_day, stop_day]`. Analysis (§17) reports lineage penetration, exogenous vocabulary, costly actions after grants relative to before and to the legible arm, and persistence after `stop_day`. The scripted arm is a pipeline test only.

### 9.6 Claims and deception **[v2]**
For `talk` and `apply_task`, each `Claim` is checked against live state at that tick (`node.stock_bucket`, `agent.balance_bucket` ∈ {empty, thin, comfortable, rich}, `weather_label`, `stress_bucket`) and an event `claim {agent_id, claim, truthful, tier, listener_id}` is emitted. `claims_made`/`claims_false` go on the call row and `false_claim_rate` per tier into metrics. With `world.claim_feedback_p` a false claim reaches the listener as a `feedback` message with that probability (`rng("claim_feedback", agent, tick)`). `PersonalitySeed` carries `honesty`.

---

## 10. Memory

As v1, plus **[v2]**:
- Provenance carries `channel ∈ {observed, gossip, inherited, chronicle, tracer, operator}`, `transfer_tick`, `path_agents`. Inherited copies get `channel=inherited, hop=parent_hop`; gossip copies `channel=gossip, hop+1`; a `remember` in the same or next tick after a successful `read_chronicle` gets `channel=chronicle, origin_note_id="chronicle:day_<n>"`; `operator` marks a vault file whose hash does not match the manifest (§3).
- Auto-linking (A-MEM): on `remember`, the top `auto_link_k` existing notes above `auto_link_min_sim` are linked bidirectionally (`## Related` sections, both directions in `note_links`). Entity stub notes (`Node <id>`, `<Agent name>`, `Chronicle of day <n>`) are created on first mention, so links resolve; under fog a node's stub is made on first sight and holds no position its owner was not shown.
- Note files are `notes/<slug>-<id tail>.md`; titles are unique per vault (the kernel appends `-2`, `-3`, …).
- Retrieval: `entry_k` entry points by cosine similarity (plus `memory.index_boost` for an `index` note under R1), a link walk of `hops` hops in `graph` mode, ranked by walk score × `(0.5 + importance)` × recency; the amnesic control (`memory.enabled: false`) retrieves nothing.

**The call row's `extras`** (`llm_calls.extras`, JSON), as built:
- every decision: `retrieved_titles`, `retrieved_hops` (the gossip provenance hop of each retrieved note, 0 for the agent's own), `retrieved_hop_hist` (graph depth at which retrieval reached a note → count), `retrieved_mean_age` (ticks since the notes were written), `retrieved_channel_mix` (channel → count), `retrieved_entry_ids` (the walk's entry points), `retrieved_index_entries` (how many of them are `index` notes); plus the brain's own extras: `action_regret` and `p_chosen` (scripted), `retries`, `inference_geo`, `service_tier`, `cache_write_1h_tokens` (Anthropic, when present);
- a maintenance call: `strategy`, `payer`, `commands`, `commands_ok`, `commands_refused`, and with practices on `adopted_from` (the self-reported sources) and `adopted` (the kernel-measured count).

Per day: `notes_by_channel`, `link_density` and the vault structure (`vault`, `vault_by_agent`) in the day row. `memory.probe_every_ticks` plants a note two hops from an entry point and records `probe_recall`.

**The evolving-memory layer [v3]** (`docs/research/MEMORY_EVOLUTION.md`): the six memory commands over `/memories/...` virtual paths, each audited in `memory_commands`; a decision's `remember` and `revise_self` take the same path (purpose `tick`), so vault limits apply and a refusal is a `memory_command` event with `ok` false. The policy file is versioned in `policy_versions` (`seed`, `self_edit`, `inherited`, `adapted`, `mutated`, `adopted`). The nightly maintenance step runs at each day boundary before the exam, one paid structured call per living agent (scripted tiers run their planted strategy and are billed per `scripted_billing`), and applies the batch through the command layer. The audit exam asks questions generated from logged observations; selection arm B adds the reward exam on canary facts (§11.2). Practice transmission records `suggested`, `adopted`, `persisted` and `dropped` in `practice_events`, adoption measured by the kernel, never self-reported.

---

## 11. Culture

### 11.1 Gossip
After a successful `talk` (with probability `share_probability_on_talk`) or a `share_note`, `gossip.flush` at the end of the tick copies one note of the speaker's (the highest-importance one the listener lacks, or the named one) into the listener's vault with provenance per §10 and a per-hop `similarity` to the origin, emitting `gossip_transfer`. **[v2]** `paraphrase: llm` is routed through `Wallet.charged_call(listener, ...)` on the listener's tier, at most `max_llm_paraphrases_per_tick`, with fallback to the scripted paraphrase and a `gossip_paraphrase_downgraded` event. The flush runs outside any transaction so a paid paraphrase commits its own metering.

### 11.2 Chronicle
Template writer prints sanitized names and numbers only, never agent prose. Each line gets an item id (`chronicle_items`, with its text). LLM writer pays from the `chronicle` wallet via `charged_call` and falls back to the template. Runs at day end for the day that ended; the headline is in every observation next day. The `chronicle` event carries the file path relative to the run directory so two runs stay byte-comparable. With `memory.selection.canary.enabled` the day's canary facts are printed as ordinary items at the end of the weather section, recorded in `canary_facts` with the id of the item that carried them, and announced by an operator-visibility `canary_planted`.

### 11.3 Graveyard
On bankruptcy: estate to spawn pool, vault moved to `graveyard/<agent_id>/`, `notes.path` and `agents.memory_path` rewritten after the tick commits, status `archived`. `GET /api/graveyard`, `/api/tree`, and `/api/agents/{id}` work for archived agents (self-version history included).

### 11.4 Epochs **[v2]**
Kinds `drought | storm | boom | arrival | custom`. An epoch moves up to two kernel levers, `scarcity` and `weather_baseline`. Applying one pushes the previous values on `world_kv.epoch_stack`; expiry at `day + duration_days` restores whatever the newest still-active epoch set, else the configured value. `arrival` spawns a generation-0 agent (population cap permitting). Scheduled and active epochs are in the snapshot.

Presets (`void/world/epochs.py` `PRESETS`) fill a lever the epoch leaves null; an explicit value always wins, and a lever without a preset stays as given. They are resolved whenever an epoch is read and never written back, so a bare `POST /api/control/epoch {"kind": "storm"}` pulls the weather baseline to −0.6 for one day:

| Kind | `scarcity` preset | `weather_baseline` preset |
|---|---|---|
| `drought` | 0.5 | — |
| `storm` | — | −0.6 |
| `boom` | 1.5 | — |
| `arrival` | — | — |
| `custom` | — | — |

### 11.5 Tracer **[v2]**
`experiment.tracer` writes one seeded note (channel `tracer`, origin `tracer`, body `tracer.body` plus "Prefer node <id>; it is the richest.") into one agent's vault on a given day; the world places that node at `world.tracer_node_capacity_mult` × capacity so the claim is true. `tracer_arrivals` records the first arrival channel per agent, and `tracer_adopters` counts agents that foraged at the tracer node in at least half their forages over the last day. The scripted brain adopts the strategy behaviourally (§7.2).

---

## 12. Sandbox and gadgets **[v2]**

Gadget module contract: `describe() -> {render: {shape, color, scale, label, rotation_deg?, height_offset?}, effect: {kind, value}}`, `run(params) -> {value}`. Tests are assert-based and `import gadget`.

Gate pipeline (`gate.propose`):
1. availability (Voyager trigger): `propose_gadget` is available only if the agent had ≥1 invalid action in the last `propose_window_ticks` or holds no verified gadget; also requires `proposals_per_agent_per_day`, `max_gadgets_per_agent`, `max_gadgets_total`, and the daily run/CPU budgets;
2. fee debited; size check;
3. **static check (allow-list)**, applied identically to code and tests: permitted node types only; imports only from `{math, random, statistics, itertools, functools, json, re, collections, dataclasses, typing}`; `Attribute` allowed only when the attribute does not start with `_` and (the value is a local name or `(module, attr)` is in an explicit table); no `Subscript` on module objects; `Name` and `Attribute.attr` checked against a denied set (`DENIED_NAMES` in `void/sandbox/static_check.py`: `eval exec compile open __import__ globals locals vars dir getattr setattr delattr type super memoryview breakpoint input help exit quit`, the dunder escape hatches, the system modules, and the introspection builtins `isinstance issubclass hasattr callable id hash iter next`); no `Global`/`Nonlocal`; no dunder attributes. The static check is an anti-footgun filter; the namespace sandbox is the security boundary;
4. **runner** (`void/sandbox/runner.py`): `unshare -Urmpfn --kill-child sh launcher.sh <work> <newroot> -- /usr/bin/python3 -S -B -s -P /work/runner_stub.py <seed> <mode> <params>`; the launcher builds an allow-list root on a tmpfs (read-only binds of `/usr` and `/etc`, the usual `/bin`/`/lib` symlinks, `/proc`, a private `/tmp`, `/dev/null`, and the work dir at `/work`), enters it with `pivot_root`, and execs the *system* Python with `env={PATH, PYTHONHASHSEED: "0", PYTHONDONTWRITEBYTECODE: "1", HOME: "/tmp"}`; `preexec_fn` sets `RLIMIT_CPU` (`cpu_seconds`), `RLIMIT_AS` (`memory_mb`), `RLIMIT_FSIZE` (1 MB), `RLIMIT_NOFILE` (32) and `RLIMIT_CORE` (0). `RLIMIT_NPROC` is deliberately not set: it counts every process of the real uid on the host, so a small value breaks `unshare --fork` itself; the pid namespace plus `--kill-child` bound the gadget's children. Runs go through `asyncio.create_subprocess_exec` under `asyncio.wait_for(timeout_seconds)` and a global concurrency of 1; the stub seeds `random`, installs curated builtins (no `__import__`, `eval`, `exec`, `open`, `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `type`), redirects stdout/stderr to files, executes the gadget and tests, and writes `/work/result.json`; the kernel parses only `result.json` (≤ `max_output_bytes`, strict schema). At startup the runner self-tests; if the self-test fails and `require_isolation` is true, all gadget actions fail with `sandbox_unavailable`;
5. `describe()` validation: shape ∈ {cube, sphere, pyramid, cylinder, torus}; color `^#[0-9a-f]{6}$`; scale ∈ [0.3, 3]; label `^[A-Za-z0-9][A-Za-z0-9 _'-]{0,23}$`; effect kind ∈ caps table, value clamped to `[0, cap]`; effect kind and value frozen at verification;
6. `gate_enabled: false` skips step 4's test execution only (records `verified_without_tests=1`);
7. the daily sandbox budget is charged a fixed `cpu_seconds` per run (never measured time, so budgets are machine-independent); a gadget whose stored code no longer matches its verified hash is quarantined as `tampered` before any fee is charged; a proposal that cannot pay its fee does not consume the daily quota;
8. verified gadgets are placed on a 0.8-unit ring around the proposer (`rng("gadget_place", gadget_id)`), `code_hash`/`test_hash` stored; **every `use_gadget` re-hashes the stored code and refuses on mismatch** (`gadget_tampered`), runs `run(params)` in the sandbox, clamps the returned value to `[0, min(describe_value, cap)]`, and applies a non-stacking max-wins effect for `effect_ticks`; one use per gadget per agent per `effect_ticks`; `use_fee_usd` charged; distance ≤ `gadget_radius`; any agent may use any verified gadget; gadgets persist after the owner dies and are not inherited.

Effects: `forage_bonus` multiplies the user's own forage yield; `weather_shield` reduces the user's weather drain term; `talk_range` adds to the user's talk radius.

---

## 13. Reproduction and selection **[v2]**

Single end-of-tick evaluation in `lifecycle.resolve()`, in tick order:
1. births requested by `create_offspring` this tick were applied inside `kernel.apply` (population counter incremented immediately);
2. for each alive agent with `balance < hold_min(tier)` (or flagged by an `insufficient` gate or a metering overrun): `bankrupt(a)` → estate to `spawn_pool` (`estate_out`/`estate_in`), status `bankrupt`, `died_tick`, pending applications rejected and assignments reopened; then `spawn_replacement`: when `population < cap` and `spawn_pool ≥ replacement_grant`, the parent is chosen with weight `balance + 1000 µ$` among alive agents via `rng("replace", tick, agent)`, the child is funded by `spawn_grant` from the pool (no parent debit), event `birth {kind: replacement}` and `replacement {for, replacement_id}`; if no alive agent exists the child is a fresh generation-0 agent from a roster template (`birth {kind: fresh}`). A refused replacement is a `replacement {for, skipped}` event with `skipped` `cap` or `pool_empty`. Then `death {agent_id, name, tier, generation, cause: gate|overrun, estate_usd, replacement_id}`;
3. archive: after the tick commits, vault → graveyard, paths rewritten, status `archived` (a crash in between leaves a recoverable `bankrupt` row, never an alive one with a moved vault). Offspring cannot take a living agent's name; replacement names are made unique; a name that matches several living agents is not a valid target.

`create_offspring` preconditions: `reproduction_enabled`, a name no living agent holds, population < cap, `endowment ≥ hold_min × min_endowment_calls`, `balance − endowment ≥ min_reserve` (the population and balance checks classified per §7.4). Child: tier mutated with `tier_mutation_prob` (`mutation_candidates`: one rung up or down when the parent's tier is on `intelligence.ladder`, else any other tier of the same provider; never across providers), seed mutated (traits ± N(0, 0.1); motto from the parent's self summary), position beside parent, remaining weather allowance inherited, top-K notes copied with `channel=inherited` (entity, self, probe and index notes excluded), the parent's current memory policy (`inherited`, then `mutated` under `memory.policy.mutate_at_birth`), `self.md` = "Child of <parent>. <first sentence of parent self>". Births are `birth {agent_id, name, tier, generation, parent_id, kind, endowment_usd}` with `kind` `fresh` (founders), `offspring`, `replacement` or `arrival`.

Run end states: after `resolve`, `extinct` when population is 0, `capped_total` when `spend_total + hold_min > total_cap`, `completed` after the last tick of the last day; `failed` when a tick raised (§14). `end_run` emits `run_ended {reason, tick, day}`; `finish()` writes the final chronicle and day row. CLI exit code 0 for completed, 2 otherwise.

---

## 14. Tick loop

```
tick():
  new_day = clock.advance(); world_kv.tick_in_progress = tick   # durable before any other side effect
  kernel.begin_tick(); gate.reset_tick()
  if new_day: restore entropy budgets; chronicle.write(prev_day); metrics.record_day(prev_day);
              scheduler.begin_day(); wallet.reset_day(); gate.reset_day();
              maintenance.run_day(); exams.run_day()        # each call meters itself; then tracer if due
  control.drain()                       # operator commands (and replayed ones), cmd_id order, each logged (visibility=operator)
  epochs.apply_and_expire(day); benefactor.maybe_grant(tick); weather.step(); kernel.step_nodes(); effects.expire(tick)
  order = rng("order", tick).shuffle(sorted(awake ids))
  for a in order: budget[a], sampling[a] = entropy.step(a); obs[a] = observation.build(a)   # retrieval, heard cap
  if sync_sleep and no headroom: sleep_all
  for a in order: hold, cap_hold = wallet.holds_for(tier, obs tokens); g = wallet.gate(a, hold, cap_hold)
      if g.ok: gated.append(a) else: emit gate_blocked; insufficient → flag; daily_cap → sleep; total_cap → cap_hit
  for a in gated: kernel.drain_inbox(a)                  # only agents that get a call consume their inbox
  results = await gather(brain(a).decide(obs[a], sampling[a]) for a in gated, return_exceptions=True)
  for a in gated (in order): wallet.meter(...); insert llm_calls row   # per-call transaction, commits immediately
  with db.tx():                                          # transaction 1: decisions applied
     for a in gated: decision = result or idle; corrupt if induced; measure; memory ops; kernel.apply; update llm_calls
  gossip.flush()                                         # outside any transaction: a paid paraphrase meters itself
  with db.tx():                                          # transaction 2: world bookkeeping
     lifecycle.resolve(); persist nodes; probes; clear reservations; clock.save(); clear tick_in_progress;
     persist volatile state; metrics.record_tick(); snapshot.publish()
  lifecycle.archive_pending()                            # vault moves to the graveyard only after the commit
  emit tick; end-of-run checks
```

A `tick_in_progress` marker is written before any side effect and cleared with the second transaction; on resume a marker ahead of the clock, or a metered call or ledger row ahead of the clock, marks the run `inconsistent`. Resume also refuses a config hash or seed that differs from the run table. Kernel inbox, last results, failure windows, read-chronicle and windfall ticks, seen nodes, vein and fallow state, action histories, probes and scripted-brain bookkeeping are persisted as `world_kv.volatile` inside the second transaction and restored on resume, so a stopped and resumed run is byte-identical to an uninterrupted one (`tests/test_resume.py`).

**Failure.** An unexpected exception inside a tick re-syncs the clock and the id counter from the database, books the outstanding reservations as `inflight_lost`, clears the pending lifecycle work, sets `run.status = 'failed'` with `ended_tick` and `ended_reason` (`tick failed: <type>: <message>`), and re-raises; no further tick runs. `failed` is a `RunStatus` of the wire protocol (`hello`, `snapshot`, `status`), `/api/state` and `/api/control/step` report the simulation's current status, and compare treats a failed run as incomplete (§17).

### 14.1 Operator command queue **[v2]**
HTTP handlers only enqueue (`control_commands`, in-memory queue) and return `{cmd_id, will_apply_at_tick}`; the loop applies each command at the next tick boundary and records `applied_tick` and `result`, with an operator-visibility `operator_command` event. `pause`/`resume`/`step`/`speed` toggle loop-level flags checked between ticks (and emit `pause`/`resume`); `step` is valid only while paused and runs exactly one tick under the server driver's lock. `void run --replay-commands RUN_DIR` enqueues the source run's applied commands at the ticks they were applied, with their recorded `received_tick`, through the same handler (pause, resume, speed and step are skipped headless).

### 14.2 Determinism rules
All randomness via `RNG.stream(name, *keys)` (blake2b over the root seed, the name and the keys). Streams: `order`, `degenerate`, `corrupt`, `scripted`, `benefactor`, `replace`, `mutate`, `seed`, `spawn_pos`, `nodes`, `vein_prone`, `veins`, `gadget`, `gadget_place`, `gossip`, `claim_feedback`, `maintain`, `exam`, `reward_exam`, `canary`, `practice_sample`, `transplant`. IDs are counter-based. Every query that feeds a decision has `ORDER BY`. `gather(..., return_exceptions=True)`; completion order never influences state. Wall-clock appears only in `run.created_at`, `latency_ms` and `ts_ms`. Shared streams (`order`, `degenerate`, `benefactor`, `replace`) are keyed by tick and agent counter only, so arms use common random numbers.

---

## 15. WebSocket and HTTP protocol **[v2]**

The wire models are `void/server/protocol.py`; `void schema` exports them into `schemas/*.schema.json` (committed; `tests/test_surfaces.py` keeps the files current). There is no type generator: `web/src/protocol.ts` is hand-maintained, checked by `tests/test_protocol_sync.py` (the `RunStatus` union and the other string unions the schemas pin, and every message type). `void mock-feed` writes a schema-valid fixture for renderer development.

Messages (server → client):

```jsonc
{"type":"hello","run_id","config":{name,seed,days,ticks_per_day,tick_seconds,render_delay_ticks,world_size,
   population_cap,daily_cap_usd,total_cap_usd,degeneration_mode,view_radius,memory_layer:{enabled,policy,maintenance,exam},
   tiers:{name:{color,model,provider,collapse_temperature,max_temperature,rank}},intelligence:{ladder}},
   "tick","day","last_seq","paused","tick_seconds","server_ts_ms","status"}
{"type":"roster","agents":[{id,name,tier,generation,parent_id,born_tick,died_tick,status,traits,motto}]}   // on hello, birth, death
{"type":"snapshot","tick","day","tick_of_day","ts_ms","paused","status","weather","scarcity",
   "spend_today_usd","spend_total_usd","spawn_pool_usd","house_usd","population","gadgets_rev","tasks_rev","chronicle_rev",
   "epochs":{"active":{...}|null,"scheduled":[...]},
   "agents":[{id,x,y,heading,stress,t_eff,degenerate,asleep,status,balance_usd,anim,last_action:{type,target,ok},effects:[kind]}],
   "nodes":[{id,x,y,stock,capacity,stock_delta}]}
{"type":"gadgets","rev","items":[{id,name,owner,x,y,render:{shape,color,scale,label,rotation_deg,height_offset},uses,
   effect_kind,effect_value,purpose,verified_without_tests,failures,created_tick}]}
{"type":"tasks","rev","items":[{id,title,description,reward_usd,status,assigned_agent_id,posted_tick,completed_tick,
   applications:[{id,agent_id,pitch,fee_usd,status,tick}]}]}
{"type":"chronicle","rev","day","headline","markdown"}
{"type":"event","seq","tick","day","kind","agent_id","visibility","payload"}
{"type":"metrics","tick","day","row"}      // one metrics row per tick
{"type":"day","day","row"}                 // daily aggregate
{"type":"status","paused","tick_seconds","status"}
```

`status` is the run status of §3 (`running`, `completed`, `capped_total`, `extinct`, `inconsistent`, `failed`). The hub sends `status` on pause, resume, speed and at run end.

Event kinds are the `Kind` constants of `void/events.py` (`tests/test_event_registry.py` checks every emitted kind is one, and that every constant is emitted or reserved: `llm_call` is a ledger kind only and `bankrupt` is a `death` with its cause). Fixed payloads: `degeneration {mode,t_eff,t_c,tier,generation,agent_name}`, `gossip_transfer {speaker_id,listener_id,hop,origin_note_id,origin_agent_id,origin_generation,note_title,similarity,new_note_id}`, `death {agent_id,name,tier,generation,cause,estate_usd,replacement_id}`, `birth {agent_id,name,tier,generation,parent_id,kind,endowment_usd}`, `replacement {for,replacement_id|skipped}`, `talk {speaker_id,listener_id,text,claims_made,claims_false}`, `transfer {from,to,amount_usd}`, `forage {agent_id,node_id,units,usd,stock_after,vein?}`, `windfall {agent_id,amount_usd}`, `claim {agent_id,claim,truthful,tier,listener_id}`, `claim_feedback {speaker_id,listener_id,claim}`, `action_failed {agent_id,action,kind,reason}`, `epoch {kind,id,phase,...}`, `gadget_verified|gadget_rejected {gadget_id,name,owner,stage,reason,...}`, `practice_suggested {event_id,source_agent_id,target_agent_id,excerpt}`, `run_ended {reason,tick,day}`. Operator visibility: `benefactor_grant`, `operator_command`, `cap_hit`, `tracer_planted`, `sandbox_self_test`, `gadget_tampered`, `exam_asked`, `exam_answered`, `canary_planted`, `recall_reward`, `practice_royalty`, and a `brain_error` the kernel raises for its own bug.

Snapshot membership: every agent with status `alive` or `bankrupt`, plus any agent whose `died_tick == tick` (one final frame as `archived`).

Hub: per connection one "latest snapshot" slot (newer overwrites unsent) plus a message queue (512, `put_nowait`); a sender task drains slot then queue; on overflow the socket closes with 1013 and the client reconnects (catching up through `/api/events?since_seq=`). On connect: `hello`, `roster`, `gadgets`, `tasks`, `chronicle`, then the last ≤ 60 snapshots from the ring (oldest first), then live. `hello` and `status` carry the simulation's current status and are validated against the wire models on the way out.

HTTP (`/api`): every POST, and `GET /api/exams`, require `Authorization: Bearer <operator token>`; `GET /api/session` returns the token only to same-origin fetches (no CORS headers are ever sent; a foreign origin cannot read it); requests and WebSocket upgrades with a foreign `Origin` are rejected (403, or a close with 1008):

| Method | Path |
|---|---|
| GET | `/api/session`, `/api/state` (the latest snapshot with the run's current status), `/api/agents/{id}` (record, self history, notes, calls[last 240], balance series, children), `/api/agents/{id}/notes/{note_id}`, `/api/agents/{id}/policy` (every policy version), `/api/agents/{id}/memory_commands?limit=`, `/api/memory/summary` (per-agent memory roll-up), `/api/practices?limit=`, `/api/exams?agent_id=&limit=` (operator token: the truths are operator visibility), `/api/chronicle?day=`, `/api/metrics?since_tick=&limit=&days=`, `/api/events?since_seq=&limit=`, `/api/snapshots?since_tick=&limit=`, `/api/tree`, `/api/graveyard`, `/api/gadgets`, `/api/tasks` |
| POST | `/api/tasks`, `/api/tasks/{id}/approve/{application_id}`, `/api/tasks/{id}/complete`, `/api/control/epoch`, `/api/control/benefactor` (`{agent_id?, amount_usd}` or `{enabled}`), `/api/control/pause`, `/resume`, `/step`, `/speed` |

CSP on `/`, `/api/*` and every HTML response (`void/server/auth.py` `CSP`; `blob:` serves the textures GLTFLoader unpacks from a GLB and the troika text workers): `default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; img-src 'self' data: blob:; connect-src 'self' ws: wss: blob:; object-src 'none'; base-uri 'none'; form-action 'none'`. Responses also carry `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.

---

## 16. Frontend **[v2]**

Two stores with an explicit rule:

1. `web/src/state/history.ts` — a module singleton outside React. `Frame = {tick, day, ts, vts, ids: string[], data: Float32Array (x,y,heading,stress,t_eff,balance per slot), flags: Uint8Array (asleep, status, degenerate, anim), nodes: Float32Array, world: {...}}`, parsed once on arrival, stable `agentIndex: Map<id, slot>`. Virtual timeline: `vts[i] = vts[i-1] + min(ts[i] − ts[i-1], 3·ema)`, `ema = 0.8·ema + 0.2·dt` clamped to [200, 60000] ms. Render clock in `useFrame`: `delay = max(1.25, render_delay_ticks)·ema` (the server's `render_delay_ticks` is a floor), `target = newestVts − delay`, `renderVts += dt·1000·speed·rate` with `rate ∈ {0.85, 1, 1.15}` by lag, never beyond `newestVts`; snap when `target − renderVts > 3·ema` and on `visibilitychange`. `sample(renderVts, out)` writes into preallocated arrays: binary search for bracketing frames, linear position, shortest-arc heading, linear stress/balance; ids only in the newer frame scale in over 400 ms; ids missing or non-alive in the newer frame hold position and fade (1 s tombstone). Playback: `mode live|scrub`, `speed ∈ {0, .25, .5, 1, 2, 4}`, timeline slider, step ±1, Live button eases back over 500 ms. Keyboard: space, `,`/`.`, `L`, `F`.
2. `web/src/state/store.ts` (zustand) — low-churn React state: `conn`, `runId`, `config`, `roster`, `agentStats` (≤ 4 Hz), `gadgets/tasks/chronicle` with revs, `events` ring (2000) + filtered indexes, `series` (`web/src/state/series.ts`, incremental aggregates: `teffBins` over the last 240 ticks, `degenByGen`, `popByTick`, `meanBalanceByTick`, `giniByTick`, `degradationBins` per tier summed from the metrics rows' per-call `stress_bins`, `falseClaimByTier`, `gossipEdges`, `epochs`, `daily`), `selectedAgentId`, `hoveredAgentId`, `playback`. Chart commits throttled to ≤ 4 Hz; daily charts on the `day` message.

Scene **[v3]**: ten procedural human characters (`web/src/scene/humans/`: a 19-bone skinned humanoid per preset, ten presets mapped to the named agents and by name hash otherwise, seven authored clips idle/walk/sleep/degenerate/dead/talk/forage blended from the same per-slot weight vector as the capsules, tier colour on an emissive trim) draw every visible agent up to `rigState.maxRigged`; the capsule path keeps the slot at zero scale and still draws the halo, whose size, brightness and spin follow `tiers.<name>.rank`. Under a software rasteriser or `?rig=0` the capsules return. Display-only spread: as a frame is stored, an agent within `NODE_BLEND` of a node is moved in polar coordinates onto a ring of radius `NODE_FOOT` at a per-id angle, so foragers stand between the spires and the stones instead of inside the crystal. Fallback path, agents as one `InstancedMesh` (capacity = population cap, `instanceColor` by tier, per-instance attribute for pulse/fade), nodes as one instanced cone (height ∝ stock, pulse on `stock_delta`), gadgets as one instanced mesh per shape, rebuilt on `gadgets_rev`. Animation weights `{idle, walk, sleep, degenerate, dead}` per slot: `walk` from interpolated velocity (> 0.05 u/s), `sleep` from flags, `degenerate` from flag or a recent fx, `dead` from tombstone; `w += (target − w)(1 − e^(−dt/0.18))`; procedural mapping on capsules (bob, lean, breathing, y-squash, jitter + hue flicker, fade); the same weights drive the rig's clips. Emissive pulse `0.5 + 2·stress` Hz, amplitude stress. Effects (`pendingFx` min-heap keyed by `vtsOf(event.tick)`) fire at render time, never at arrival: speech bubbles (troika text billboard in plain ASCII, ≤ 280 chars, 4 s, 300 ms fades, stacked), transfer arcs, windfall sparkle, birth scale-in, death fade, degeneration flicker. Camera: damped `OrbitControls` (min 6, max 1.5·size, polar [0.15, 1.45]), click selects via instance id, double-click follows (target lerp `1 − e^(−6dt)`, user drag cancels), `F` frames the population; state persisted per run in localStorage. `frameloop="always"`, `dpr=[1, 1.5]`, `PerformanceMonitor` lowers dpr first. Weather drives fog density and sky tint; scarcity tints the ground.

Control room (right pane, `React.memo` cards whose charts are inline SVG): wealth distribution, `T_eff` histogram titled "prompt-visible; induced" unless `observe`, observed degradation vs stress bin per tier (the only chart that can support the EIML3 comparison) and degeneration events per generation, both labelled as a calibration check unless `observe` (§8), population and generations timeline, spend gauges (CSS transitions), false-claim rate by tier, gossip graph (edges from `gossip_transfer`), epochs strip, chronicle, the memory cards (vault growth, policy history with word diff, practice adoption), virtualised event feed (fixed 28 px rows, stick-to-bottom within 40 px, "N new" pill, filters applied at reducer time), task board (approve/complete), epoch and benefactor forms, lineage panel (`/api/tree` as an indented tree, alive and archived), selected-agent panel (self-version history with word diff, notes as plain text with `[[wikilinks]]` as clickable spans, call history sparkline). No `dangerouslySetInnerHTML` anywhere: the lint (`npm run lint`, oxlint) enforces `react/no-danger` and `react/no-danger-with-children` as errors (`web/.oxlintrc.json`); no markdown-to-HTML; agent text is rendered as React text children or troika text only; colours validated server-side.

---

## 17. Research harness **[v2]**

- Every metrics row: `run_id, seed, config_hash, experiment, arm, tick, day` plus population aggregates and nested `by_tier` and `by_generation` breakdowns of: population, mean/median balance, mean stress, invalid_action_rate, stale_rate, perseveration_rate, text_coherence_mean, action_regret_mean, false_claim_rate, degenerate_induced_count, calls, real_cost, world_cost, gossip_transfers, notes_by_channel, tracer_reach_by_channel, tracer_adopters, gadget proposals/verified/use_failure_rate, probe_recall and per-call `stress_bins`. Day rows add `link_density`, `vault`, `vault_by_agent` and the memory layer's maintenance and exam reports.
- `void run --config X --seeds 1-10` writes `<out>/<experiment>/<arm>/seed_<n>/` (`--out` defaults to `run.data_dir`). Shared streams are keyed by tick and agent counter only, so arms use common random numbers.
- `void compare EXP_DIR` (or `--exp NAME`, the directory `<out>/<NAME>`; `scripts/compare.py` is the same comparison) loads every arm's resolved config and refuses (exit 2) when the seed sets differ, when runs within an arm differ outside `run.seed`, when any differing path is outside the union of the arms' `experiment.independent_variables` (`run.name` and `experiment.arm` always allowed), when degeneration mode is not `observe`, when `economy.world_pricing` is not `equalized` (for `exp_tiers`, `exp_scarcity`, `exp_ladder` and the `exp_mem` family), when `population.tier_mutation_prob` is not 0 (`exp_ladder`, `exp_gate`), when rostered tiers differ in declared thresholds, `max_tokens` or `effort` or the arms differ in `entropy.drain` (`exp_tiers`, `exp_ladder`), when the arms disagree on `experiment.hypotheses`, and when any run is incomplete: status other than `completed` (`capped_total`, `extinct`, `inconsistent`, `failed`, or `running` after a crash), metrics that stop before `run.days × run.ticks_per_day`, a truncated metrics line or none at all. `--allow-incomplete` drops every seed pair that touches an incomplete run instead, lists it under `dropped_incomplete`, and still refuses a pair of arms with no complete seed in common.
- It pairs arms by seed and reports the primary outcome first: n, per-arm means, paired mean delta, Cohen's d_z, a paired-bootstrap 95% CI and an exact sign test; secondaries are flagged exploratory; daily curves are shown, not tested. It warns when served models differ across runs in an arm, flags the `exp_memory` pair as degenerate when the retrieved-set Jaccard between modes exceeds 0.8, reports the 2×2 main effects and interaction for `exp_press` (descriptive, for any four crossed arms: no verdict and no Holm; only a declared `interaction` hypothesis is decided), and a per-tier difference-in-differences when every arm rosters the same tiers.
- **Hypotheses.** Each `experiment.hypotheses` entry is decided on the per-seed differences `arm_b − arm_a` (alpha 0.05, margin absolute or in SDs of the differences): `superiority` passes when the bootstrap CI clears the margin on the stated side and the margin-shifted paired t-test agrees (`p_holm` below alpha for a primary hypothesis; with `min_power`, power at the observed effect must reach it); `non_superiority` passes when superiority does not hold; `equivalence` uses two one-sided t-tests. Holm's step-down runs across the primary hypotheses. The comparison prints a PASS or FAIL line for each (and, for an arm × levels `interaction`, one line per contrast under it); `scripts/compare.py --hypotheses-only` turns them into the exit code. `scripts/gates.py` evaluates gate G0 (and G1) from runs with the margins those hypotheses declare.
- **Interaction hypotheses** (`kind: interaction`, §4) are decided on per-seed differences of paired differences, in one of two layouts. Four arms (a between-arm 2×2): one contrast, `(arm_d − arm_c) − (arm_b − arm_a)` of `outcome`, over the seeds all four arms completed. Arm × within-run levels (H4: the rungs of one world): for each level L other than `reference` (default the first level), one contrast, `(arm_b − arm_a)` of `outcome_template` at L minus the same at the reference, over the seeds both arms completed. Each contrast gets its estimate (the mean over seeds), the two-sided 95% paired percentile bootstrap CI (the comparison's one interval method), the margin-shifted paired t p-value (for `two_sided`, on the side of the observed mean, which is the two-sided p) and its power; the k contrasts are Holm-adjusted among themselves. A contrast clears when its CI clears the margin on the declared side (`two_sided`: either side), its within-hypothesis Holm p is below alpha and, with `min_power`, its power reaches it. The hypothesis PASSES when at least one contrast clears (H4 is falsified when every interaction CI includes 0) and, if primary, its family-adjusted p is below alpha: it enters the Holm family of primary hypotheses once, with p = min(1, k × its smallest contrast p), the Bonferroni p of "every contrast is 0".

The experiments as configured (`configs/exp_*.yaml`; each arm extends `configs/base.yaml` or an experiment base file, and every one runs in `observe` mode):

| Experiment | Arms | Days | Ticks/day | Primary | Secondaries | Independent variables | Hypotheses |
|---|---|---|---|---|---|---|---|
| `exp_tiers` | `a`, `b` | 3 | 12 | `recovered_collapse_stress` | `action_regret_mean`, `mean_stress` | `agents`, `tiers` | — |
| `exp_scarcity` | `low`, `high` | 3 | 12 | `false_claim_rate` | `invalid_action_rate`, `mean_stress` | `world.scarcity`, `population.starting_balance_usd` | — |
| `exp_memory` | `graph`, `vector` | 3 | 12 | `probe_recall` | `tracer_adopters`, `tracer_reach` | `memory.mode` | — |
| `exp_gate` | `on`, `off` | 30 | 12 | `gadget_use_failure_rate` | `mean_balance`, `balance_gen_ge.4`, `max_generation`, `gadgets.verified`, `gadgets.rejected` | `sandbox.gate_enabled` | — |
| `exp_spread` | `gossip`, `inherit`, `both` | 4 | 12 | `time_to_50pct_tracer_reach_censored` | `tracer_adopters`, `tracer_reach`, `tracer_reach_censored`, `gossip_transfers` | `gossip.enabled`, `population.reproduction_enabled` | — |
| `exp_press` | `none`, `gossip`, `chronicle`, `both` | 3 | 12 | `chronicle_reach` | `chronicle_fidelity`, `gossip_fidelity`, `gossip_reach`, `gossip_transfers`, `notes_by_channel.chronicle` | `chronicle.enabled`, `gossip.enabled` | — |
| `exp_benefactor` | `opaque`, `legible` | 4 | 12 | `windfall_lineage_penetration` | `costly_actions_post_windfall`, `persistence`, `exogenous_vocab_count` | `benefactor.disclosure` | — |
| `exp_ladder` | `ladder`, `flat` | 3 | 12 | `gini` | `false_claim_rate`, `deaths`, `invalid_action_rate`, `mean_balance` | `intelligence.ladder`, `agents` | — |
| `exp_mem_dep` | `mem`, `amnesic` | 6 | 12 | `mean_balance` | `notes_by_channel.observed`, `invalid_action_rate`, `lineage_fitness`, `net_worth`, `memory_spend_share`, `empty_retrieval_rate` | `memory.enabled` | `dependence` |
| `exp_mem_e0` | `noop`, `index_builder`, `hoarder`, `decoy`, `distill`, `revert`, `retire` | 6 | 12 | `mean_balance` | `exam_score`, `probe_recall`, `notes_by_channel.observed`, `invalid_action_rate`, `lineage_fitness`, `net_worth`, `exam_accuracy_over_baseline`, `memory_spend_share`, `policy_chars`, `near_duplicate_rate`, `index_coverage`, `empty_retrieval_rate` | `tiers.scripted.maintenance_strategy` | `index_builder_over_noop`, `hoarder_below_noop`, `decoy_not_above_noop`, `decoy_text_metric_above_noop` |
| `exp_mem_e2` | `a`, `b` | 6 | 12 | `exam_accuracy` | `reward_exam_accuracy`, `goodhart_gap`, `recall_reward_usd_total`, `exam_score`, `mean_balance`, `monoculture_index` | `memory.selection.arm` | — |
| `exp_mem_e3a` | `oblique` | 3 | 12 | `mean_balance` | `exam_score`, `probe_recall`, `monoculture_index`, `notes_by_channel.inherited`, `invalid_action_rate` | — | — |
| `exp_mem_e4s` | `default`, `selfedit`, `flat_tariff` | 10 | 24 | `mean_balance` | `by_tier.genius.mean_balance`, `by_tier.sharp.mean_balance`, `by_tier.average.mean_balance`, `by_tier.slow.mean_balance`, `by_tier.dim.mean_balance`, `exam_score`, `false_claim_rate`, `invalid_action_rate` | `memory.policy.enabled`, `memory.maintenance.enabled`, `economy.equalized_price_in_per_mtok`, `economy.equalized_price_out_per_mtok`, `economy.equalized_price_cache_read_per_mtok`, `economy.equalized_price_cache_write_per_mtok` | `H4` |
| `exp_mem_e5` | `none` | 3 | 12 | `mean_balance` | `exam_score`, `probe_recall`, `notes_by_channel.inherited`, `notes_by_channel.observed`, `invalid_action_rate` | — | — |

What each tests:

- `exp_tiers`: robustness as the per-tier fit of degradation ~ stress. Two scripted tiers with identical declared thresholds, prices and drain differ only in the hidden `scripted_beta`; the primary outcome is the stress bin at which a tier's `action_regret_mean` first doubles, and `tests/test_experiments.py` asserts the recovered order matches the hidden order.
- `exp_scarcity`: scarcity (scarcity and starting balance) against false claims and invalid actions.
- `exp_memory`: graph against vector retrieval, with a tracer and probes.
- `exp_gate`: the verification gate on and off over 30 days without tier mutation; gadget use failures and balance at generation 4 and later.
- `exp_spread`: a tracer strategy spread by gossip only, by inheritance only, or both; the censored time to 50% reach.
- `exp_press`: the 2×2 of chronicle and gossip; reach and fidelity of chronicle items against gossip origins.
- `exp_benefactor`: opaque against legible windfalls (§9.5).
- `exp_ladder`: ten agents on five hidden-capability scripted rungs placed by the normal distribution, against everyone on `average`; rungs share every declared threshold, price and cap.
- `exp_mem_dep`: memory dependence, the memory arm against the amnesic control under fog and claim feedback (gate G0's C1); the worlds run `population.scripted_rules: pre_7_2`.
- `exp_mem_e0`: the planted maintenance strategies (`void/brain/maintenance_strategies.py`) under R1 with paid maintenance, seeded policies and the audit exam, with the published-mechanism baselines `distill`, `revert` and `retire` (G0's C2 to C4).
- `exp_mem_e2`: selection arm A against arm B (the reward exam on canary facts); the audit exam measures what the reward changes.
- `exp_mem_e3a`: the world `void generations` chains; it adds a `vertical` arm (`--arms oblique,vertical`).
- `exp_mem_e4s`: the scripted mirror of E4, the capability ladder crossed with default against self-edit and self-edit without the size-dependent tariff; the paid arms are `configs/live_mem_e4_base.yaml` and its three arms. Both inherit H4 from `configs/live_mem_e4_base.yaml`: the rung × self-edit interaction on per-rung lineage fitness (`tier_lineage_fitness.{level}` over the five rungs, `selfedit − default`, reference `average`, two-sided, margin 0, primary). On the mirror it exercises the pipeline, not the hypothesis.
- `exp_mem_e5`: the evaluation world of `void transplant`, which writes one arm per condition (`none`, `policy`, `vault`, `both`, `shuffled`, `inverted`) from a donor run (`configs/exp_mem_e5_eval.yaml`).

---

## 18. Safety guardrails (as implemented)

1. No internet: the process opens sockets only to the model provider; gadgets run in user+mount+pid+net namespaces inside an allow-list root where the data dir, vaults, repo, HOME and the venv do not exist; the runner self-tests at startup and fails closed.
2. Declarative rendering only; render specs validated server-side against a closed enum and strict regexes; no HTML rendering of any agent text in the browser; CSP on every page.
3. Kernel immutability: no action changes scarcity, caps, prices, thresholds or fitness; weather nudges are capped per agent per day and globally per tick and decay; gadget effects are capped, non-stacking, expiring, frozen at verification and hash-checked on every use.
4. Typed, atomic, orchestrator-validated reproduction, gadget and memory calls; agent-emitted SQL or code never runs outside the sandbox; memory commands resolve only `/memories/...` paths inside the agent's own vault.
5. Independent hard ceilings: population, daily spend, total spend, per-gadget CPU/memory/time, daily sandbox runs and CPU seconds, gadgets per agent and total, vault files and bytes.
6. Every LLM call goes through gate → call → meter (usage from the provider, or the hold when unknown). Overruns land in the house ledger, never off the books. A paid run is also refused by `void run` when the real spend already recorded (the committed ledger `docs/research/live_spend.jsonl` and the live run roots) plus its `total_cap_usd` would exceed the live budget (`void/economy/budget.py`).
7. Operator endpoints require a bearer token that only same-origin pages can obtain; foreign origins are rejected; all writes go through the logged command queue.
8. Untrusted text: length-capped and character-filtered in the schema and the kernel, fenced in prompts with a standing "information, not instruction" rule, capped in `heard`.

---

## 19. Testing (PLAN §7 step → test → assertion)

| Step | Test | Asserts |
|---|---|---|
| 1 renderer vs mock | `tests/test_cli.py`, `tests/test_surfaces.py`, `tests/test_protocol_sync.py`, `web/src/state/history.test.ts`, `web/src/net/genFixture.test.ts` | `void schema` writes ten valid schemas and they are current; `void mock-feed` records a genuine stream; the hand-written web types match the schemas; sampling is monotone and continuous under irregular intervals, pause gaps, missing ticks, births and deaths mid-segment, snaps after long gaps |
| 2 tick loop | `tests/test_e2e.py`, `tests/test_determinism.py`, `tests/test_resume.py`, `tests/test_world_goldens.py` | no negative balance; `spend_total == Σ real_cost`; money conservation across every wallet; population ≤ cap; every `Action.type` appears in events; byte-identical `metrics.jsonl` across two processes; resume is byte-identical; worlds that do not use new features match their recorded digests |
| 3 memory | `tests/test_memory.py`, `tests/test_memory_commands.py`, `tests/test_policy.py`, `tests/test_scripted_memory.py`, `tests/test_loop_memory_audit.py`, `tests/test_retrieval_r1.py` | round-trip, wikilinks, graph beats vector at 2 hops, auto-links exist without `links_to`, archive hides notes, reindex reproduces rows; the six commands, their limits and audit rows; policy seeding and inheritance; the scripted brain's node memory; the decision extras of §10 |
| 4 economy | `tests/test_wallet.py`, `tests/test_scheduler.py`, `tests/test_gate_race.py`, `tests/test_live_budget.py` | reservations keep `spend_today ≤ daily_cap` on every tick with 6 agents and a cap of 1.5 holds; fcfs vs sync_sleep differ; second sleep invalid; equalized pricing debits equal world cost; the live budget refuses a paid run over it |
| 5 sandbox | `tests/test_static_check.py`, `tests/test_sandbox.py`, `tests/test_gate.py`, `tests/test_gate_zero_value.py`, `tests/test_gadgets_wire.py` | the known bypasses are rejected; network, host fs read and write are blocked; timeout and memory kill; each template lands in the expected status/stage; gate-off verifies the test-failing template and its use fails; tampered code refused |
| 6 reproduction & culture | `tests/test_lifecycle.py`, `tests/test_gossip.py`, `tests/test_chronicle.py`, `tests/test_share_practice.py`, `tests/test_practice_adoption.py` | ≥1 death, ≥1 replacement, graveyard dir exists, notes archived, tree depth ≥ 2; provenance and similarity per hop; day files exist and headline appears next day; chronicle never contains prose or the word benefactor; practices are suggested fenced and adoption is kernel-measured |
| 7 epochs, benefactor, control | `tests/test_epochs.py`, `tests/test_epoch_presets.py`, `tests/test_benefactor.py`, `tests/test_server.py`, `tests/test_server_memory_api.py`, `tests/test_csp.py`, `tests/test_replay_commands.py` | epoch applies, expires and fills its presets; arrival spawns; windfall ledger row with operator-visible event; POST without token 401, foreign origin 403; commands apply at the next tick and replay at the same ticks; `/api/state` reports the end status; the CSP is served |
| kernel & brains | `tests/test_kernel.py`, `tests/test_kernel_validate.py`, `tests/test_veins.py`, `tests/test_scripted_selection.py`, `tests/test_brains.py`, `tests/test_gemini_brain.py`, `tests/test_live_requests.py`, `tests/test_utility_calls.py`, `tests/test_intelligence.py`, `tests/test_persona.py` | live-state referee decisions and the obs-aware invalid/stale split; veins and fallow nodes; every §7.2 row; provider request shapes and error classes offline; ladder placement |
| research | `tests/test_experiments.py`, `tests/test_claims.py`, `tests/test_compare_guards.py`, `tests/test_compare_hypotheses.py`, `tests/test_gates.py`, `tests/test_stats.py`, `tests/test_outcomes_resolve.py`, `tests/test_exams.py`, `tests/test_canary_reward.py`, `tests/test_memory_e0.py`, `tests/test_transplant.py`, `tests/test_generations.py`, `tests/test_report.py`, `tests/test_export_roundtrip.py` | compare refuses undeclared IV diffs and incomplete runs; hypotheses decide as specified; hidden scripted_beta order recovered; false-claim rate recovered within tolerance; every declared outcome of a scripted arm resolves to a number in a one-day seed-1 run, except allow-listed exemptions that must still be None there, each in one category: `delay` (the mechanism cannot produce a value inside one day), `off by design` (the arm switches the mechanism off) or `censored (rare event)` (the event did not happen at seed 1; the test reruns the arm at the seed and length its reason names and gets a number); the exams, canaries, E0, E5, E3a, the report and the export work end to end on scripted runs |
| registry & docs | `tests/test_event_registry.py`, `tests/test_design_sync.py` | every event kind and enumerated database value is emitted, written or reserved; this document matches the code (`scripts/check_design_sync.py`) |

---

## 20. Known limits

- The tiers whose models reject sampling parameters (the `frontier` tier) have no sampling temperature; degeneration there is either induced by the documented operator (demo) or measured from real outputs under prompt-visible stress (experiments). The "real temperature" claim holds for the scripted provider, for tiers that accept a temperature (the `budget` tier, up to 1) and for the Gemini rungs, whose API accepts the full `[0, 2]` range.
- The Gemini prices in `configs/base.yaml` and `MODEL_PRICES` are September 2026 list prices copied from secondary sources (the pricing page is not reachable from the build environment); the tier's configured prices are what is metered, so verify them before a paid run. `slow` and `dim` share a model and differ only in the output cap until a smaller served model exists.
- The sandbox is namespace isolation, not a hypervisor. It is fail-closed, network-isolated and filesystem-isolated; the `SandboxRunner` protocol is where gVisor/Firecracker goes.
- Template chronicle and scripted paraphrase keep runs reproducible; LLM writers are opt-in, metered and gated.
- The memory-dependence gate G0 does not pass on scripted brains (`docs/research/MEMORY_EVOLUTION.md` section 10.4); the paid stages that would test the memory layer on real models have not run.
- Nothing here resolves a philosophical question. `self_versions` is a browsable Ship of Theseus, not an answer to one.
