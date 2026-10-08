# Runbook

## Prerequisites

- Python 3.11+, `uv` (or pip), Node 22+.
- Linux with unprivileged user namespaces (`unshare -Urmpfn` must work) for the gadget sandbox. Without it the sandbox fails closed: gadget actions are rejected with `sandbox_unavailable`, everything else runs. Set `sandbox.require_isolation: false` only if you accept running gadget code without filesystem isolation (not recommended).
- For live tiers: `ANTHROPIC_API_KEY` (or a profile from `ant auth login`) for Claude tiers; `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) for Gemini tiers.

## Install

```bash
uv venv .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
```

## Run

| Goal | Command |
|---|---|
| Watch a scripted world | `.venv/bin/void serve --config configs/demo.yaml` then open `http://127.0.0.1:8000` (add `--paused` to start paused, `--tick-seconds 1` to slow it, `--static web/dist` if you built elsewhere) |
| Frontend only, no server | `cd web && VITE_FEED=fixture npm run dev` (replays the recorded fixture) |
| Headless scripted run | `.venv/bin/void run --config configs/scripted_smoke.yaml --days 3 --out data/runs` |
| Live two-tier run | `ANTHROPIC_API_KEY=... .venv/bin/void serve --config configs/live_two_tier.yaml` |
| Live Gemini capability ladder (ten agents, 1/2/4/2/1 by a normal distribution) | `GEMINI_API_KEY=... .venv/bin/void serve --config configs/live_gemini_ladder.yaml` (the resolved rungs print at start; `--seed 7` moves the genius) |
| Resume a run | add `--resume` (a run whose tick counter is behind its metered calls is marked `inconsistent` and refuses) |
| Slow down or pause | in the control room, or `POST /api/control/speed {"tick_seconds": 1.5}`, `/pause`, `/resume`, `/step` |
| Export protocol schemas | `.venv/bin/void schema --out schemas/` |
| Record a fixture for the frontend | `.venv/bin/void mock-feed --config configs/scripted_smoke.yaml --ticks 240 --out web/public/fixtures/mock.jsonl` |
| Inspect a run | `.venv/bin/void inspect --run-dir data/runs/demo --tree` (also `--money`, `--events N`, `--reindex`) |
| Evolving-memory layer (fog, policy file, nightly maintenance, audit exam) | see the section below; configs `exp_mem_e0_*.yaml` (scripted controls) and `live_mem_e1_*.yaml` (the pilot) |

The server prints the operator token once at startup (or set `VOID_OPERATOR_TOKEN`). The web app fetches it from `/api/session`, which only answers same-origin requests; every POST needs `Authorization: Bearer <token>`.

## Frontend notes

- `npm run build` writes `web/dist`, which `void serve` serves at `/`. `npm run typecheck`, `npm run test` (vitest) and `npm run lint` are the checks.
- On a machine with a GPU the scene runs with MSAA, ambient occlusion, bloom and a vignette at dpr up to 1.5; on a software renderer it drops to Lambert shading, no composer and dpr 0.75 automatically.
- Keys: space pause/resume playback, `,`/`.` step, `L` live, `F` frame the population, `Esc` clear selection; click selects, double-click follows.
- Assets: `web/public/models/rig.glb` (ReadyPlayerMe or Mixamo export with idle/walk/sleep clips) and `web/public/models/manifest.json` (prop pack) are picked up automatically when present; `web/README.md` documents the formats.

## Run directory layout

```
data/runs/<name>/
  world.db               agents, ledger, llm_calls, events, notes index, tasks, gadgets, snapshots, metrics
  config.resolved.yaml   the exact config (its sha256 is in the run table)
  metrics.jsonl          one row per tick plus one per day, each with run_id, seed, config_hash, experiment, arm
  vaults/<agent_id>/     self.md and notes/*.md (open the folder in Obsidian)
  graveyard/<agent_id>/  vaults of dead agents
  chronicle/day_NNN.md   the town record
  gadgets/<gadget_id>/   verified and rejected gadget sources (hash-pinned)
```

## Experiments

Each `configs/exp_*.yaml` extends `base.yaml`, declares its `experiment` block (name, arm, independent variables, primary and secondary outcomes) and runs in `entropy.degeneration.mode: observe`. Run every arm over the same seeds, then compare:

```bash
for arm in a b; do .venv/bin/void run --config configs/exp_tiers_$arm.yaml --seeds 1-10 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_tiers
.venv/bin/python scripts/analyze.py runs/exp_tiers/a/seed_1 --json
```

`compare` refuses runs whose configs differ outside the declared independent variables, whose seed sets differ, or that were not run in `observe` mode. It pairs arms by seed and reports the primary outcome first with n, paired delta, Cohen's d_z, a paired-bootstrap 95% CI and a sign test; secondaries are flagged exploratory.

## The evolving-memory layer

The thin slice of [docs/research/MEMORY_EVOLUTION.md](research/MEMORY_EVOLUTION.md) (sections 5 and 10.4). Every piece is a config switch, off by default, so `base.yaml` runs exactly as before.

| Switch | What it does | Where to look afterwards |
|---|---|---|
| `run.log_prompts: true` (default) | stores the full user turn, a hash of the system prompt (`prompt_texts`) and the observation as JSON on every `llm_calls` row | `SELECT prompt_text, observation FROM llm_calls`; `void.research.export.export_run(run_dir)` writes every table to CSV or Parquet |
| `world.view_radius: 12.0` | fog of war: nodes and inhabitants beyond the radius are not listed; a node's entity stub is created on first sight, never at birth; scripted agents move to remembered node positions | `events` kind `move`; `notes` tagged `entity` per agent equal the nodes it has seen |
| `world.claim_feedback_p: 0.5` | after a checkable false claim the listener hears, with that probability, that it was false | `events` kinds `claim` and `claim_feedback`; `[feedback]` lines in the heard section of the prompt |
| `memory.enabled: false` | the amnesic control: `remember` is dropped and nothing is retrieved; the self-summary stays | no `remember` events, only `entity` notes |
| `memory.policy.enabled: true` | each agent owns `vaults/<id>/memory_policy.md`, seeded from `seed: blank | default | diverse` and inherited at birth; shown fenced in the user turn under "Your memory policy (written by you)" with the one system-prompt carve-out | `policy_versions` (one row per version with source and similarity to the parent) |
| `memory.maintenance.enabled: true` | one paid structured call per living agent between days: the model (or a scripted strategy, `tiers.<scripted>.maintenance_strategy`) returns a batch of memory commands applied under `rights` | `llm_calls` purpose `maintain`, ledger kind `maintenance_call`, `memory_commands`, events `maintenance` / `maintenance_skipped` / `memory_command` |
| `memory.exam.enabled: true` | the audit exam: questions generated from what the agent was shown `delay_days` earlier, graded by exact match, never rewarded, provider calls paid by `economy.research_pool_usd` | `exam_items`, `exam_answers`, operator events `exam_asked` / `exam_answered`; outcomes `exam_score`, `exam_abstain_rate` |

The six memory commands (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) match the shape of Anthropic's memory tool; paths are virtual (`/memories/notes/`, `/memories/index/`, `/memories/memory_policy.md`, `/memories/self.md`) and confined to the agent's own vault (`void.memory.commands.resolve_path`). The database row stays authoritative for provenance; every kernel write is hashed into `note_manifest`, and `reindex` believes a file only when the hash matches (a hand edit in Obsidian is admitted as channel `operator`, not hidden).

**E0, the controls before any money is spent** (scripted, free): four arms that differ only in the planted maintenance strategy.

```bash
for arm in noop index_builder hoarder decoy; do .venv/bin/void run --config configs/exp_mem_e0_$arm.yaml --seeds 1-20 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_e0        # primary: mean_balance; expect index_builder > noop > hoarder, decoy = noop
```

**The live pilots** (need `GEMINI_API_KEY`; prices in `base.yaml` are list prices to verify before paying; every run is capped by `economy.total_cap_usd`):

| Pilot | Command | Rough cost `[estimate]` |
|---|---|---|
| Scarcity and deception on a live tier (`sharp`), 10 seeds | `for arm in high low; do .venv/bin/void run --config configs/live_scarcity_$arm.yaml --seeds 1-10 --out data/exp; done` then `scripts/compare.py data/exp/exp_scarcity_live` | about $30 (about $5 on `average`) |
| E1, does self-directed memory help (`average`), 10 seeds x 4 arms | `for arm in a0 a b c; do .venv/bin/void run --config configs/live_mem_e1_$arm.yaml --seeds 1-10 --out data/exp; done` then `scripts/compare.py data/exp/exp_mem_e1` | about $100 |

Read the E1 result in this order (section 6.3 of the proposal): `a` must beat `a0` (memory matters under fog at all), then `c` versus `b` is the question; `c` versus `a` is the effect net of its cost.

## Tuning the economy

`VoidConfig.warnings()` (printed by the CLI) flags any roster tier whose estimated call cost is outside 0.3 to 2.0 times the forage income per tick, and a total cap that binds before the last day. The levers: `world.forage_yield_usd`, `world.node_regen_per_tick`, `world.scarcity`, `population.starting_balance_usd`, `population.min_reserve_usd`, tier prices, `economy.world_pricing: equalized`.

## Safety switches

- `sandbox.enabled: false` disables gadgets entirely; `sandbox.gate_enabled: false` skips test execution (research question 4) but never the static check or spec validation.
- `economy.daily_cap_usd` and `economy.total_cap_usd` are real-dollar ceilings enforced by reservation before every call.
- `benefactor.enabled`, `epochs`, task posting and approvals are operator actions logged as `operator_command` events.
