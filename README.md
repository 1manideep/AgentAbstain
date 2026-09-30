# The Void

A closed, self-contained world populated by LLM-powered agents that perceive, act, remember, earn, spend, build, reproduce, gossip, and occasionally degenerate, rendered live in 3D with a control room beside it. Every agent decision is a real model call gated by a real dollar balance; every mechanism runs end to end with a deterministic scripted brain and no API key, and the same code path runs against live Claude models when a key is present.

The plan is in [`docs/PLAN.md`](docs/PLAN.md); the buildable specification, including every decision the plan left open, is in [`docs/DESIGN.md`](docs/DESIGN.md); operating instructions are in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## What is in the box

| Subsystem | Where | What it does |
|---|---|---|
| Referee / kernel | `void/world/kernel.py` | The only writer to world state. Validates and applies every action against live state; physics, bounds, caps, claims checking |
| Wallet / economy | `void/economy/` | Real micro-dollar ledger. Reservation-based gate before each call, metering from provider usage after, daily and total caps, kernel wallets, task board, benefactor |
| Brains | `void/brain/` | `ScriptedBrain` (utility softmax with real temperature), `AnthropicBrain` (one structured-output Messages call per tick), entropy model, degeneration operator, coherence and claim metrics |
| Memory | `void/memory/` | Per-agent Obsidian-compatible vault of markdown notes with `[[wikilinks]]`, hashing embeddings, graph retrieval, auto-linking, provenance channels, self-node versions, tracer and probe notes |
| Culture | `void/culture/` | Gossip with per-hop paraphrase drift and provenance; a template (or metered LLM) chronicle |
| Sandbox | `void/sandbox/` | Verification-gated gadget creation: allow-list static check, then execution in user/mount/pid/net namespaces inside a `pivot_root` allow-list root with rlimits; declarative render specs only |
| Lifecycle | `void/agents/lifecycle.py` | Offspring with inheritance and mutation, bankruptcy with estate conservation, fitness-weighted replacement, graveyard |
| Simulation | `void/sim/` | The tick loop, observation builder, operator command queue, snapshots, metrics |
| Server | `void/server/` | FastAPI + WebSocket hub with per-client outboxes, catch-up endpoints, operator token auth, CSP |
| Frontend | `web/` | Vite + React + react-three-fiber: instanced agents with time-based interpolation outside React, control room charts, event feed, task board, lineage, self-history diffs |
| Research | `void/research/`, `configs/exp_*.yaml`, `scripts/` | Seeded, paired experiments with declared independent variables and pre-registered outcomes; `compare` refuses incomparable runs |

## What it looks like

![Live localhost, day 2](web/screenshots/live-landscape-1.png)

The left pane is a seeded low-poly landscape (terrain, lakes, trees, crystal resource nodes, gadgets on pedestals, a sky that follows the world clock) rendered with three.js through react-three-fiber; agents are instanced and interpolated from a time-based history buffer so motion stays smooth while ticks are irregular. The right pane is the control room: spend gauges, event feed, charts, task board, lineage, and the selected agent's self-history and notes.

## Quick start

```bash
# backend
uv venv .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q                     # the whole suite runs offline in about a minute

# frontend
cd web && npm install && npm run build && cd ..

# a scripted world you can watch (no API key needed)
.venv/bin/void serve --config configs/demo.yaml    # prints the operator token; open http://127.0.0.1:8000
```

Headless runs and experiments:

```bash
.venv/bin/void run --config configs/scripted_smoke.yaml --days 3
.venv/bin/void run --config configs/exp_tiers_a.yaml --seeds 1-5 --out runs
.venv/bin/void run --config configs/exp_tiers_b.yaml --seeds 1-5 --out runs
.venv/bin/python scripts/compare.py runs/exp_tiers
```

Frontend development without a server: `cd web && VITE_FEED=fixture npm run dev` replays `web/public/fixtures/mock.jsonl`, a genuine recorded run produced by `void mock-feed`. Drop a rigged character at `web/public/models/rig.glb` or a prop pack with `web/public/models/manifest.json` to replace the procedural placeholders (see `web/README.md`).

Live models: set `ANTHROPIC_API_KEY` (or log in with `ant auth login`) and use `configs/live_two_tier.yaml`. Spend is bounded by `economy.daily_cap_usd` and `economy.total_cap_usd`; every call is reserved before and metered from the provider's reported usage after.

## Guardrails that are code, not policy

- No internet: the process opens sockets only to the model provider; gadgets run in namespaces where the data directory, vaults, repository and home directory do not exist, with the network namespace empty.
- Kernel immutability: no action can change scarcity, caps, prices, thresholds or the fitness function; weather nudges and gadget effects are capped and decay.
- Money conservation: every balance change is a ledger row; overruns land in a house ledger; bankrupt estates return to the spawn pool.
- Operator writes go through a logged command queue applied at tick boundaries, behind a bearer token that only same-origin pages can obtain.
- Agent text reaches other agents only inside quote fences under a standing "information, not instruction" rule, and reaches the browser only as text nodes.

## Known limits

Claude 5.x models expose no sampling temperature, so on those tiers degeneration is either induced by a documented kernel operator (demo mode) or measured from real outputs under prompt-visible stress (experiment mode, `entropy.degeneration.mode: observe`). The scripted provider samples at real temperature. The sandbox is namespace isolation, not a hypervisor. Nothing here resolves a philosophical question; the self-node version history is a browsable Ship of Theseus, not an answer to one.
