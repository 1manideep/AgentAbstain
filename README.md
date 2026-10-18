# The Void

A closed, self-contained world populated by LLM-powered agents that perceive, act, remember, earn, spend, build, reproduce, gossip, and occasionally degenerate, rendered live in 3D with a control room beside it. Every agent decision is a real model call gated by a real dollar balance; every mechanism runs end to end with a deterministic scripted brain and no API key, and the same code path runs against live Claude models when a key is present.

The plan is in [`docs/PLAN.md`](docs/PLAN.md); the buildable specification, including every decision the plan left open, is in [`docs/DESIGN.md`](docs/DESIGN.md); operating instructions are in [`docs/RUNBOOK.md`](docs/RUNBOOK.md). Flowcharts of every subsystem are in [`docs/DIAGRAMS.md`](docs/DIAGRAMS.md). The flagship research proposal, evolving memory practices under ecological selection, is in [`docs/research/MEMORY_EVOLUTION.md`](docs/research/MEMORY_EVOLUTION.md).

## What is in the box

| Subsystem | Where | What it does |
|---|---|---|
| Referee / kernel | `void/world/kernel.py` | The only writer to world state. Validates and applies every action against live state; physics, bounds, caps, claims checking |
| Wallet / economy | `void/economy/` | Real micro-dollar ledger. Reservation-based gate before each call, metering from provider usage after, daily and total caps, kernel wallets, task board, benefactor |
| Brains | `void/brain/` | `ScriptedBrain` (utility softmax with real temperature), `AnthropicBrain` and `GeminiBrain` (one JSON-schema-constrained call per tick; Gemini takes real temperature over `[0, 2]`), entropy model, degeneration operator, coherence and claim metrics |
| Memory | `void/memory/` | Per-agent Obsidian-compatible vault of markdown notes with `[[wikilinks]]`, hashing embeddings, graph retrieval, auto-linking, provenance channels, self-node versions, tracer and probe notes |
| Culture | `void/culture/` | Gossip with per-hop paraphrase drift and provenance; a template (or metered LLM) chronicle |
| Sandbox | `void/sandbox/` | Verification-gated gadget creation: allow-list static check, then execution in user/mount/pid/net namespaces inside a `pivot_root` allow-list root with rlimits; declarative render specs only |
| Lifecycle | `void/agents/lifecycle.py` | Offspring with inheritance and mutation, bankruptcy with estate conservation, fitness-weighted replacement, graveyard |
| Simulation | `void/sim/` | The tick loop, observation builder, operator command queue, snapshots, metrics |
| Server | `void/server/` | FastAPI + WebSocket hub with per-client outboxes, catch-up endpoints, operator token auth, CSP |
| Frontend | `web/` | Vite + React + react-three-fiber: ten procedural human characters (skinned, seven-clip movement set) driven by a time-based history buffer outside React, capability halos, control room charts, event feed, task board, lineage, self-history diffs |
| Research | `void/research/`, `configs/exp_*.yaml`, `scripts/` | Seeded, paired experiments with declared independent variables and pre-registered outcomes; `compare` refuses incomparable runs |

## What it looks like

![Live localhost, day 3: the inhabitants at the nodes](web/screenshots/humans-world.png)

The left pane is a seeded low-poly landscape (terrain, lakes, trees, crystal resource nodes, gadgets on pedestals, a sky that follows the world clock) rendered with three.js through react-three-fiber. The inhabitants are ten procedural human characters, one per named agent (build, skin tone, hair, clothes and accessories differ; the tier colour is the emissive trim and the halo above the head, whose size and spin follow the capability rung), skinned to a 19-bone rig with an authored seven-clip movement set: idle, walk, sleep, degenerate, dead, talk and forage. Positions come from a time-based history buffer interpolated outside React, so motion stays smooth while ticks are irregular; agents standing on a node are shown at the crystal's foot. The right pane is the control room: spend gauges, event feed, charts, task board, lineage, and the selected agent's self-history and notes.

![Dev, the genius, foraging](web/screenshots/humans-forage.png)

![The ten characters](web/screenshots/humans-contact.png)

![One character, seven moves](web/screenshots/humans-moves.png)

Gallery: open `#/characters` on a built frontend (`?moves=1&preset=N` for the move strip). Under a software rasteriser the characters fall back to the instanced capsules (`?rig=1` forces them on).

## Quick start

```bash
# backend
uv venv .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -p no:warnings; echo "exit $?"   # offline; read the exit code (see below)

# frontend
cd web && npm install && npm run build && cd ..

# a scripted world you can watch (no API key needed)
.venv/bin/void serve --config configs/demo.yaml --out runs   # prints the operator token; open http://127.0.0.1:8000
```

The test suite needs no key and no network. It is not quick: a full run took 15 minutes of wall time on a 4-core machine, and 26 minutes while other jobs shared it (measured 2026-10-18); one file at a time is quicker (`.venv/bin/python -m pytest tests/test_cli.py`). `pyproject.toml` already passes `-q` (`addopts`), so the line above prints dots and a one-line summary; a second `-q` (as in `CLAUDE.md`) hides the summary too, and either way the exit code is the verdict (0 passed, 1 a test failed).

The demo world is written to `runs/demo/` and lasts 14 days of 24 ticks at 0.6 s a tick (about three and a half minutes); the server keeps serving it after the last tick. A run directory is never started over silently, so once the demo has run, the same command prints `run dir runs/demo already holds a run: pass --resume to continue it, --fresh to archive it and start over, or --out DIR` and exits 2. Then:

```bash
.venv/bin/void serve --config configs/demo.yaml --out runs --resume   # reopen it: a stopped run continues, a finished one is shown as it ended
.venv/bin/void serve --config configs/demo.yaml --out runs --fresh    # rename it to runs/demo.prev-1 and start a new one
```

`--resume` needs the same config and seed as the run it reopens (the config hash is checked, `--tick-seconds` included); `--fresh` takes the first free `runs/demo.prev-<n>`. Both work the same way for `void run`.

One output root: every run in this README is written under `runs/` (`--out runs`), which is gitignored. Without `--out`, `void run` and `void serve` write to the config's `run.data_dir` (`data/runs/`, also ignored); live runs that spend real money go to `data/live/` (`docs/RUNBOOK.md`).

Headless runs and experiments (all scripted, so free and deterministic):

```bash
.venv/bin/void run --config configs/scripted_smoke.yaml --days 3 --out runs        # runs/scripted_smoke/
.venv/bin/void run --config configs/exp_tiers_a.yaml --seeds 1-5 --out runs        # runs/exp_tiers/a/seed_<n>/
.venv/bin/void run --config configs/exp_tiers_b.yaml --seeds 1-5 --out runs
.venv/bin/python scripts/compare.py runs/exp_tiers
.venv/bin/void run --config configs/exp_ladder_ladder.yaml --seeds 1-5 --out runs  # one genius, most not
.venv/bin/void run --config configs/exp_ladder_flat.yaml --seeds 1-5 --out runs    # everyone average
.venv/bin/python scripts/compare.py runs/exp_ladder                                # primary: gini
```

Frontend development without a server: `cd web && VITE_FEED=fixture npm run dev` replays `web/public/fixtures/mock.jsonl` and `api.json`, a genuine recorded run of `configs/demo_memory.yaml` produced by `void mock-feed` (whose defaults are that config and that file). Drop a rigged character at `web/public/models/rig.glb` or a prop pack with `web/public/models/manifest.json` to replace the procedural placeholders (see `web/README.md`).

Live models: two providers are built in. The Anthropic tiers are `frontier` and `budget` in `configs/base.yaml` (`configs/live_two_tier.yaml`, and the `configs/live_claude_*.yaml` test kit that `scripts/live_claude.sh` runs; `ANTHROPIC_API_KEY`); the Gemini tiers are the five ladder rungs (`configs/live_gemini_ladder.yaml`; `GEMINI_API_KEY` or `GOOGLE_API_KEY`). Spend is bounded by `economy.daily_cap_usd` and `economy.total_cap_usd`; every call is reserved before and metered from the provider's reported usage after. A config that puts agents on a paid Anthropic tier is refused by `void run` and `void serve` unless `--budget-usd` is given, and is then counted against the live budget together with every recorded run and the committed spend ledger; real spend goes through `scripts/live_claude.sh` (`docs/RUNBOOK.md`, "Live Claude test").

## The command line

`.venv/bin/void <command> --help` documents every option.

| Command | What it does |
|---|---|
| `void run` | a headless run of one config; `--seeds 1-20` sweeps seeds into `<out>/<experiment>/<arm>/seed_<n>/`; `--resume`, `--fresh`, `--replay-commands RUN_DIR` |
| `void serve` | the same run behind the HTTP/WebSocket server and the built frontend (`--paused`, `--tick-seconds`, `--port`) |
| `void compare` | paired comparison of an experiment's arms (`void compare runs/<experiment>`, as `scripts/compare.py`); refuses an unfair comparison with exit 2 |
| `void analyze` | the summary of one run (as `scripts/analyze.py`; `--json`) |
| `void export` | one file per table of a run plus a data card (`manifest.json`); CSV, or Parquet with the `export` extra |
| `void report` | a self-contained static HTML report of a run or of an experiment (`--out FILE`) |
| `void transplant` | E5: a donor run's memory policy and notes transplanted into fresh agents of an evaluation world, one arm per condition |
| `void generations` | E3a: worlds chained generation by generation, the top half surviving and the children inheriting a policy |
| `void mcp` | the six memory commands over the Model Context Protocol (stdio) for one vault directory |
| `void inspect` | look inside a run directory (`--tree`, `--money`, `--events N`, `--policy AGENT`, `--maintenance`, `--exams`, `--reindex`) |
| `void schema` | write the wire JSON Schemas (`--out schemas/`) |
| `void mock-feed` | record a scripted run as the message stream a socket would receive (the frontend's fixture) |

The research scripts sit beside it: `scripts/compare.py` (also `--hypotheses-only`), `scripts/analyze.py`, `scripts/gates.py` (gates G0 and G1), `scripts/mem_diagnosis.py` (the memory-dependence diagnosis), `scripts/code_practices.py`, and the live kit `scripts/live_claude.sh` with `scripts/live_budget.py`. `docs/RUNBOOK.md` shows each in use.

## Not everyone is equally smart

`intelligence.ladder` names tiers from smartest to dumbest; agents listed without a `tier` are placed on it by a normal distribution (`void/intelligence.py`): ten agents on five rungs come out **1 / 2 / 4 / 2 / 1**, one genius and one dunce guaranteed, and which personality is the genius rotates with `run.seed` (so a paired-by-seed experiment averages personality out of the intelligence effect). The resolved roster is part of the config hash and is printed at start (`intelligence: genius=1 (Dev) · sharp=2 (Hex, Juno) · ...`). Children mutate one rung up or down, never across a provider. In the scene the halo says the rung: wide, bright and fast for the genius, small and dull for the dunce.

| rung (tier in `base.yaml`) | provider | thinking | out cap | list price in / out per MTok | ≈ real cost per call | offline stand-in (`demo.yaml`) |
|---|---|---|---|---|---|---|
| `genius` | Gemini | 2048 | 1536 | $2.00 / $12.00 | $0.011 | scripted, β 3.0, defect 0.10 |
| `sharp` | Gemini | 1024 | 1280 | $0.75 / $3.75 | $0.003 | scripted, β 2.0, defect 0.20 |
| `average` | Gemini | off | 1024 | $0.25 / $1.50 | $0.0007 | scripted, β 1.0, defect 0.30 |
| `slow` | Gemini | off | 640 | $0.10 / $0.40 | $0.0002 | scripted, β 0.6, defect 0.45 |
| `dim` | Gemini | off | 320 | $0.10 / $0.40 | $0.0002 | scripted, β 0.35, defect 0.60 |

The model each rung is served by is the `model` field of its tier in [`configs/base.yaml`](configs/base.yaml) (`slow` and `dim` share one model and differ in their output cap); the docs name tiers, never models. Per call assumes a 1.5k-token prompt and typical thinking use; a ten-agent day at 24 ticks is about **$0.55** on the ladder against about $2.40 with everyone on the `frontier` tier or $0.60 on the `budget` tier (the two Anthropic tiers, $4 / $20 and $1 / $5). Prices are September 2026 list prices and live in the config, not the code: check them against the Gemini API pricing page on `ai.google.dev` before a paid run. Every Gemini rung takes a real sampling temperature in `[0, 2]` (`api_temperature_max: 2.0`), so `T_eff` reaches the sampler instead of only the prompt. Swap the genius for the `frontier` tier with `ladder: [frontier, sharp, average, slow, dim]`.

## Guardrails that are code, not policy

- No internet: the process opens sockets only to the model provider; gadgets run in namespaces where the data directory, vaults, repository and home directory do not exist, with the network namespace empty.
- Kernel immutability: no action can change scarcity, caps, prices, thresholds or the fitness function; weather nudges and gadget effects are capped and decay.
- Money conservation: every balance change is a ledger row; overruns land in a house ledger; bankrupt estates return to the spawn pool.
- Operator writes go through a logged command queue applied at tick boundaries, behind a bearer token that only same-origin pages can obtain.
- Agent text reaches other agents only inside quote fences under a standing "information, not instruction" rule, and reaches the browser only as text nodes.

## Known limits

A tier declared `supports_temperature: false` (the `frontier` tier in `base.yaml`) takes no sampling temperature, so on such tiers degeneration is either induced by a documented kernel operator (demo mode) or measured from real outputs under prompt-visible stress (experiment mode, `entropy.degeneration.mode: observe`). Gemini tiers and the scripted provider sample at real temperature (Gemini over its full `[0, 2]` range). The sandbox is namespace isolation, not a hypervisor. Nothing here resolves a philosophical question; the self-node version history is a browsable Ship of Theseus, not an answer to one.
