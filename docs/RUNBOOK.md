# Runbook

## Prerequisites

- Python 3.11+, `uv` (or pip), Node 22+.
- Linux with unprivileged user namespaces (`unshare -Urmpfn` must work) for the gadget sandbox. Without it the sandbox fails closed: gadget actions are rejected with `sandbox_unavailable`, everything else runs. Set `sandbox.require_isolation: false` only if you accept running gadget code without filesystem isolation (not recommended).
- For live tiers: `ANTHROPIC_API_KEY`, or a profile from `ant auth login`.

## Install

```bash
uv venv .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
```

## Run

| Goal | Command |
|---|---|
| Watch a scripted world | `.venv/bin/void serve --config configs/demo.yaml` then open `http://127.0.0.1:8000` |
| Headless scripted run | `.venv/bin/void run --config configs/scripted_smoke.yaml --days 3 --out data/runs` |
| Live two-tier run | `ANTHROPIC_API_KEY=... .venv/bin/void serve --config configs/live_two_tier.yaml` |
| Resume a run | add `--resume` (a run whose tick counter is behind its metered calls is marked `inconsistent` and refuses) |
| Slow down or pause | in the control room, or `POST /api/control/speed {"tick_seconds": 1.5}`, `/pause`, `/resume`, `/step` |
| Export protocol schemas | `.venv/bin/void schema --out schemas/` |
| Record a fixture for the frontend | `.venv/bin/void mock-feed --config configs/scripted_smoke.yaml --ticks 240 --out web/public/fixtures/mock.jsonl` |
| Inspect a run | `.venv/bin/void inspect --run-dir data/runs/demo --tree` (also `--money`, `--events N`, `--reindex`) |

The server prints the operator token once at startup (or set `VOID_OPERATOR_TOKEN`). The web app fetches it from `/api/session`, which only answers same-origin requests; every POST needs `Authorization: Bearer <token>`.

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

## Tuning the economy

`VoidConfig.warnings()` (printed by the CLI) flags any roster tier whose estimated call cost is outside 0.3 to 2.0 times the forage income per tick, and a total cap that binds before the last day. The levers: `world.forage_yield_usd`, `world.node_regen_per_tick`, `world.scarcity`, `population.starting_balance_usd`, `population.min_reserve_usd`, tier prices, `economy.world_pricing: equalized`.

## Safety switches

- `sandbox.enabled: false` disables gadgets entirely; `sandbox.gate_enabled: false` skips test execution (research question 4) but never the static check or spec validation.
- `economy.daily_cap_usd` and `economy.total_cap_usd` are real-dollar ceilings enforced by reservation before every call.
- `benefactor.enabled`, `epochs`, task posting and approvals are operator actions logged as `operator_command` events.
