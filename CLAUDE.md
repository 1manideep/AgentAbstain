# Working on The Void

A closed 3D world of LLM agents with a real-dollar economy. Backend in `void/` (Python 3.11), web in `web/` (Vite, React 19, three, R3F), research harness in `void/research/` and `scripts/`, configs in `configs/`, docs in `docs/`.

## Rules

- Work on branch `claude/trusting-goodall-ew0p38`. Never push elsewhere, never open a pull request.
- No model identifiers in code, comments, docs or commit messages (commit trailers excepted).
- Real model calls cost real money. Never call a provider outside `scripts/live_claude.sh` (or a config whose `economy.total_cap_usd` is set and counted against the live budget). The live budget is $60 in total across all runs; check the cumulative spend before starting any run.
- Never print, log, commit or write an API key into the repository.
- Prefer scripted brains (`provider: scripted`) for anything that does not need a real model: they are free and deterministic.

## Commands

```bash
.venv/bin/python -m pytest -q -p no:warnings          # backend tests (the doubled -q hides the summary; use the exit code)
.venv/bin/ruff check .                                 # lint (ruff format is not enforced)
cd web && npx vitest run --root . && npx tsc -b        # web tests and the build typecheck (tsc -b includes tests)
.venv/bin/void run --config configs/demo.yaml --seeds 1 --tick-seconds 0 --out runs   # scripted demo
.venv/bin/python scripts/compare.py runs/<experiment>  # paired comparison of arms
```

## Conventions

- Shared surfaces (`void/config.py`, `void/events.py`, `void/db.py` with its `MIGRATIONS`, `web/src/protocol.ts`, `schemas/`) change in one place per wave so parallel work does not conflict.
- Money moves only through `void/economy/wallet.py`; `Wallet.gate` and `Wallet.meter` run outside any database transaction.
- Determinism comes from named RNG streams (`rng.stream(...)`); run ids derive from the run name.
- Web text rendered with troika must be plain ASCII (missing glyphs make it fetch fonts the CSP blocks).
- Run vitest with `--root .` from `web/`.
- Every claim in `docs/research/` is either computed by a script or tagged `[assumption]`.

## Where things are tracked

- Research plan and gates: `docs/research/MEMORY_EVOLUTION.md` (section 10.4 has the current status).
- Operations: `docs/RUNBOOK.md`.
- The owner's Obsidian vault mirrors status in `digital brain/Work/Project Void/` ("Void — Status", "Void — Build Log"); the orchestrating session updates it after each wave.
