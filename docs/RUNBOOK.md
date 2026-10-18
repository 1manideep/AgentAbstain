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

## Output roots

Two roots, both gitignored:

- `runs/` for everything free (scripted brains): pass `--out runs` to `void run`, `void serve`, `void transplant` and `void generations`. One run lands in `runs/<run.name>/`; a seed sweep (`--seeds`) in `runs/<experiment>/<arm>/seed_<n>/`, which is the experiment directory `compare`, `report`, `gates.py` and `mem_diagnosis.py` read (`runs/<experiment>`).
- `data/live/` for everything that spends real money. `scripts/live_claude.sh` writes there by default, and every live budget check counts the runs under it whatever `--out` says.

Without `--out`, `void run`, `void serve`, `void transplant` and `void generations` write to the config's `run.data_dir` (`data/runs/` in `base.yaml`) and `void compare --exp NAME` looks in `data/runs/`; the commands below always pass `--out` (or a directory) so nothing lands there by accident.

## Run

| Goal | Command |
|---|---|
| Watch a scripted world | `.venv/bin/void serve --config configs/demo.yaml --out runs` then open `http://127.0.0.1:8000` (add `--paused` to start paused, `--tick-seconds 1` to slow it, `--static web/dist` if you built elsewhere) |
| Frontend only, no server | `cd web && VITE_FEED=fixture npm run dev` (replays the recorded fixture) |
| Headless scripted run | `.venv/bin/void run --config configs/scripted_smoke.yaml --days 3 --out runs` |
| Live two-tier run (paid) | `ANTHROPIC_API_KEY=... .venv/bin/void serve --config configs/live_two_tier.yaml --out data/live --budget-usd 60` (a paid Anthropic tier is refused without `--budget-usd`; reserve the run in the spend ledger first, as under "Watch the demo in the 3D viewer" below) |
| Live Gemini capability ladder (ten agents, 1/2/4/2/1 by a normal distribution; paid) | `GEMINI_API_KEY=... .venv/bin/void serve --config configs/live_gemini_ladder.yaml --out data/live` (the resolved rungs print at start; `--seed 7` moves the genius) |
| Run the same world again | a run directory that already holds a run is never started over silently: without a flag the command prints `run dir ... already holds a run: pass --resume to continue it, --fresh to archive it and start over, or --out DIR` and exits 2 |
| Resume a run | add `--resume` (same config and seed: the config hash is checked; a finished run reopens as it ended and runs no tick; a run whose tick counter is behind its metered calls is marked `inconsistent` and refuses) |
| Start over | add `--fresh`: the existing directory is renamed to `<dir>.prev-<n>` (the first free `n`) and a new run starts |
| A run directory still in use | a `void serve` or `void run` holds an exclusive lock on its run directory (`<run dir>/.void.lock`, recording its pid and command) for as long as it runs; the operating system releases it when the process exits, however it exits. While it is held, a second `void serve` or `void run` on that directory (plain, `--resume` or `--fresh`) renames and writes nothing: it prints one line and exits 2, `void serve: <dir> is in use by a running server (pid N); stop it first` (`void run: ...` from `void run`; `a running headless run (pid N)` when a `void run` holds it; `another process` when the lock file holds no readable record). Stop the holder by its pid, then rerun. In a `--seeds` sweep only the busy seed is skipped; the exit status is still 2 |
| The port is taken | `void serve` (plain, `--resume` or `--fresh`) tries to bind its host and port before it touches the run directory, so `--fresh` archives nothing when the port is busy: `void serve: port P on HOST is in use by another process; stop it or pass another --port`, or `void serve: cannot listen on HOST:P (<reason>); pass another --host or --port`, exit 2. `--port 0` means "not given" (the config's `server.port`). The check and the server's own bind are two steps, so a process that takes the port in the instant between them still makes the server fail at startup |
| Slow down or pause | in the control room, or `POST /api/control/speed {"tick_seconds": 1.5}`, `/pause`, `/resume`, `/step` |
| Export protocol schemas | `.venv/bin/void schema --out schemas/` |
| Record a fixture for the frontend | `.venv/bin/void mock-feed` (defaults `--config configs/demo_memory.yaml --ticks 240 --out web/public/fixtures/mock.jsonl`: it rewrites the committed fixture and its `api.json`; `demo_memory` turns the memory layer on so the feed carries maintenance, policy and exam events; pass `--out` elsewhere to keep the committed one) |
| Inspect a run | `.venv/bin/void inspect --run-dir runs/demo --tree` (also `--money`, `--events N`, `--policy AGENT`, `--memory-commands N`, `--maintenance`, `--exams`, `--reindex`) |
| Summarize a run | `.venv/bin/void analyze runs/demo --json` (as `scripts/analyze.py`) |
| Export a run | `.venv/bin/void export runs/demo` (see "Export and the data card" below) |
| Report on a run or an experiment | `.venv/bin/void report runs/demo --out runs/demo/report.html` (one static HTML file; an experiment directory gives the comparison) |
| Evolving-memory layer (fog, policy file, nightly maintenance, audit exam) | see the section below; configs `exp_mem_dep_*`, `exp_mem_e0_*`, `exp_mem_e2_*`, `exp_mem_e3a`, `exp_mem_e4s_*`, `exp_mem_e5_eval` (scripted) and `live_mem_e1_*`, `live_mem_e4_*` (paid) |

The server prints the operator token once at startup (or set `VOID_OPERATOR_TOKEN`). The web app fetches it from `/api/session`, which only answers same-origin requests; every POST needs `Authorization: Bearer <token>`.

## Frontend notes

- `npm run build` writes `web/dist`, which `void serve` serves at `/`. `npm run typecheck`, `npm run test` (vitest) and `npm run lint` are the checks.
- On a machine with a GPU the scene runs with MSAA, ambient occlusion, bloom and a vignette at dpr up to 1.5; on a software renderer it drops to Lambert shading, no composer and dpr 0.75 automatically.
- Keys: space pause/resume playback, `,`/`.` step, `L` live, `F` frame the population, `Esc` clear selection; click selects, double-click follows.
- Assets: `web/public/models/rig.glb` (ReadyPlayerMe or Mixamo export with idle/walk/sleep clips) and `web/public/models/manifest.json` (prop pack) are picked up automatically when present; `web/README.md` documents the formats.

## Run directory layout

```
runs/<name>/               one run (a sweep: runs/<experiment>/<arm>/seed_<n>/)
  world.db                 agents, ledger, llm_calls, events, notes index, tasks, gadgets, snapshots, metrics
  .void.lock               the run-directory lock of the process running it ({pid, command}); not run output
  config.resolved.yaml     the exact config (its sha256 is in the run table)
  metrics.jsonl            one row per tick plus one per day, each with run_id, seed, config_hash, experiment, arm
  vaults/<agent_id>/       self.md, notes/*.md and memory_policy.md (open the folder in Obsidian)
  graveyard/<agent_id>/    vaults of dead agents
  chronicle/day_NNN.md     the town record
  gadgets/<gadget_id>/     verified and rejected gadget sources (hash-pinned)
  export/                  written by `void export` (one file per table and manifest.json)
  transplant.json          E5 runs only: what was transplanted into whom
```

## Experiments

Each `configs/exp_*.yaml` extends `base.yaml`, declares its `experiment` block (name, arm, independent variables, primary and secondary outcomes, and for some the pre-registered hypotheses) and runs in `entropy.degeneration.mode: observe`. Run every arm over the same seeds, then compare:

```bash
for arm in a b; do .venv/bin/void run --config configs/exp_tiers_$arm.yaml --seeds 1-10 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_tiers           # or: .venv/bin/void compare runs/exp_tiers
.venv/bin/python scripts/analyze.py runs/exp_tiers/a/seed_1 --json
.venv/bin/void report runs/exp_tiers --out runs/exp_tiers/report.html
```

`compare` pairs arms by seed and reports the primary outcome first with n, paired delta, Cohen's d_z, a paired-bootstrap 95% CI and a sign test; secondaries are flagged exploratory. It prints `refused: <reason>` and exits 2, computing nothing, when

- the seed sets differ between arms, or two runs of one arm differ outside `run.seed`;
- an arm was not run in `entropy.degeneration.mode: observe`;
- the resolved configs differ at any path outside the arms' declared `experiment.independent_variables` (`run.name` and `experiment.arm` are always allowed); for the `exp_tiers`, `exp_scarcity`, `exp_ladder` and `exp_mem` families `economy.world_pricing` must be `equalized`, and `exp_ladder` and `exp_gate` need `population.tier_mutation_prob: 0`;
- the arms disagree on `experiment.hypotheses`;
- any run is incomplete: its status is not `completed` (`capped_total`, `extinct`, `inconsistent`, `failed`, or `running` after a crash), or its metrics stop before the last tick or are cut short.

`--allow-incomplete` (both `scripts/compare.py` and `void compare`) drops every seed pair that touches an incomplete run instead, lists the dropped runs in the warnings, and still refuses when a pair of arms has no complete seed in common.

Pre-registered hypotheses (`experiment.hypotheses`: `exp_mem_dep_base.yaml`, `exp_mem_e0_base.yaml`, `live_mem_e1_base.yaml`; `exp_mem_e5_eval.yaml` clears the list it would inherit) are decided on the paired per-seed differences and printed as one `PASS` or `FAIL` line each, with Holm's adjustment across the primary ones (the rules are in the docstring of `void/research/compare.py`). `scripts/compare.py --hypotheses-only` prints only those lines (with `--json`, their rows) and turns them into the exit code: 0 when no primary hypothesis fails (also when none is registered), 1 when one fails, 2 when the comparison is refused.

```bash
.venv/bin/python scripts/compare.py runs/exp_mem_e0 --hypotheses-only; echo "exit $?"
```

## The evolving-memory layer

The thin slice of [docs/research/MEMORY_EVOLUTION.md](research/MEMORY_EVOLUTION.md) (sections 5 and 10.4). Every piece is a config switch, off by default, so `base.yaml` runs exactly as before.

| Switch | What it does | Where to look afterwards |
|---|---|---|
| `run.log_prompts: true` (default) | stores the full user turn, a hash of the system prompt (`prompt_texts`) and the observation as JSON on every `llm_calls` row | `SELECT prompt_text, observation FROM llm_calls`; `void export RUN_DIR` writes every table to CSV or Parquet (below) |
| `world.view_radius: 12.0` | fog of war: nodes and inhabitants beyond the radius are not listed; a node's entity stub is created on first sight, never at birth; scripted agents move to remembered node positions | `events` kind `move`; `notes` tagged `entity` per agent equal the nodes it has seen |
| `world.claim_feedback_p: 0.5` | after a checkable false claim the listener hears, with that probability, that it was false | `events` kinds `claim` and `claim_feedback`; `[feedback]` lines in the heard section of the prompt |
| `memory.enabled: false` | the amnesic control: `remember` is dropped and nothing is retrieved; the self-summary stays | no `remember` events, only `entity` notes |
| `memory.policy.enabled: true` | each agent owns `vaults/<id>/memory_policy.md`, seeded from `seed: blank | default | diverse` and inherited at birth; shown fenced in the user turn under "Your memory policy (written by you)" with the one system-prompt carve-out | `policy_versions` (one row per version with source and similarity to the parent) |
| `memory.maintenance.enabled: true` | one paid structured call per living agent between days: the model (or a scripted strategy, `tiers.<scripted>.maintenance_strategy`) returns a batch of memory commands applied under `rights` | `llm_calls` purpose `maintain`, ledger kind `maintenance_call`, `memory_commands`, events `maintenance` / `maintenance_skipped` / `memory_command` |
| `memory.exam.enabled: true` | the audit exam: questions generated from what the agent was shown `delay_days` earlier, graded by exact match, never rewarded, provider calls paid by `economy.research_pool_usd` | `exam_items`, `exam_answers`, operator events `exam_asked` / `exam_answered`; outcomes `exam_score`, `exam_abstain_rate` |
| `memory.selection` (`arm: A` or `B`, `canary.enabled`) | canary facts: one templated fact a day printed unmarked in the chronicle and recorded kernel-side; arm B also asks a daily reward exam about canaries at least `reward_delay_days` old and pays `recall_reward_usd` per correct answer from the research pool | `canary_facts`, ledger kind `recall_reward`; outcomes `reward_exam_accuracy`, `goodhart_gap`, `recall_reward_usd_total` |

The six memory commands (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) match the shape of Anthropic's memory tool; paths are virtual (`/memories/notes/`, `/memories/index/`, `/memories/memory_policy.md`, `/memories/self.md`) and confined to the agent's own vault (`void.memory.commands.resolve_path`). The database row stays authoritative for provenance; every kernel write is hashed into `note_manifest`, and `reindex` believes a file only when the hash matches (a hand edit in Obsidian is admitted as channel `operator`, not hidden).

**The memory-dependence check, first** (scripted, free): the E0 world with memory on versus the amnesic control, nothing else switched on. Section 5.2 of the proposal makes it the gate: unless the amnesic arm loses, no result about memory quality means anything. It does not pass yet; see section 10.4 of the proposal.

```bash
for arm in mem amnesic; do .venv/bin/void run --config configs/exp_mem_dep_$arm.yaml --seeds 1-20 --tick-seconds 0 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_dep       # primary: mean_balance; the gate needs mem > amnesic
```

**E0, the controls before any money is spent** (scripted, free): four arms that differ only in the planted maintenance strategy. Scripted maintenance is billed flat (`memory.maintenance.scripted_billing: flat`), so the no-op pays what the others pay.

```bash
for arm in noop index_builder hoarder decoy; do .venv/bin/void run --config configs/exp_mem_e0_$arm.yaml --seeds 1-20 --tick-seconds 0 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_e0        # primary: mean_balance; pre-registered: index_builder > noop > hoarder (decoy: proposal §6.2)
```

**E0's maintenance baselines** (scripted, free; proposal 6.13 row 5): three more strategies in the same world, each an arm of `exp_mem_e0`. `distill` merges near-duplicate notes (same title stem, or cosine at least 0.9) into the newest one; `revert` restores the previous policy version after a day whose income fell (in E0 nothing else edits the policy, so it pays what `noop` pays); `retire` deletes the oldest notes beyond a cap of 40, index notes and kernel anchors exempt. Run them into the same experiment directory, so `compare` sets them against the four controls (the gate below reads only `noop`, `index_builder`, `hoarder` and `decoy`):

```bash
for arm in distill revert retire; do .venv/bin/void run --config configs/exp_mem_e0_$arm.yaml --seeds 1-20 --tick-seconds 0 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_e0
```

**Gate G0, and the diagnosis** (free, from the runs above). `scripts/gates.py` evaluates G0's four conditions (proposal 5.2, 6.2): C1 memory dependence (`mem` over `amnesic`), C2 the index builder over the no-op with power at least 0.8, C3 the hoarder below the no-op, C4 the decoy not above the no-op on balance but above it on policy length. Each condition reads its margin (and C2 its power) from the hypotheses the configs declare and names the file that declares them. It prints one line per condition and a G0 line, and exits 0 when every condition passes, 1 when any fails, 2 when none fails but one could not be evaluated (an arm without completed runs, too few pairs, runs refused). G0 does not pass on scripted brains (section 10.4 has the numbers); the command exits 1.

```bash
.venv/bin/python scripts/gates.py runs                         # reads runs/exp_mem_dep and runs/exp_mem_e0
.venv/bin/python scripts/gates.py runs --live data/live        # C1 on the live dependence stages of the Claude test instead
.venv/bin/python scripts/gates.py runs --g1 data/live/exp_mem_e1   # also gate G1 on the E1 pilot (arms a0, a, b, c)
```

`--experiments DEP,E0` names other experiment directories, `--allow-incomplete` drops the seed pairs that touch an incomplete run (listed on stderr), `--json` prints the result as JSON. `scripts/mem_diagnosis.py` gives the 10.4 diagnosis of a dependence experiment: per quantity (foraging income, decision-call cost, input tokens per decision, end balance) both arms' means, the paired difference with its bootstrap interval and `d_z`, and the break-even income rise memory would need to pay for itself. `--sweep OUT` also runs the free 10.4 sweep of node capacity, regrowth and view radius on scripted brains (it refuses any config with a paid tier) and tabulates it. Exit 0 when the table was produced, 2 when the runs were refused or missing.

```bash
.venv/bin/python scripts/mem_diagnosis.py runs/exp_mem_dep
.venv/bin/python scripts/mem_diagnosis.py runs/exp_mem_dep --sweep runs/mem_sweep --sweep-seeds 1-10 --jobs 4
```

**E2, which selection signal** (scripted, free; proposal 6.4): the E0 world under the DESIGN 7.2 scripted rules (the founders reproduce from day 1), with canary facts in the chronicle. The arms differ only in `memory.selection.arm`: `a` rewards nothing beyond survival, `b` pays $0.02 per correct reward-exam answer about canaries at least two days old. Primary `exam_accuracy`; `reward_exam_accuracy` is the manipulation check and `goodhart_gap` the Goodhart measure. In this world (canary facts in the chronicle) a scripted agent that reads the chronicle notes the page's lines, and answers a reward question from its retrieved notes: right when a note holds the line, otherwise it abstains. So arm `b` pays: `compare` shows a non-zero `recall_reward_usd_total` in arm `b` and $0 in arm `a` (at seeds 1-20, $0.4710 per run in `b`, `d_z` 3.81; proposal 10.4), while `reward_exam_accuracy` and `goodhart_gap`, which only arm `b` has, show `n/a` in the paired text output (each `b` run's value is in `--json`, under `values.b`). The scripted run checks the plumbing of the paid path (canaries, reward exam, payments, outcomes, compare), not an effect: scripted recall is positional and ignores the reward.

```bash
for arm in a b; do .venv/bin/void run --config configs/exp_mem_e2_$arm.yaml --seeds 1-20 --tick-seconds 0 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_e2
```

**E5, the practice transplant** (scripted, free; proposal 6.7): `void transplant` copies a donor run's live notes and current memory policy into the first `--recipients` (default 3) agents of the evaluation world `configs/exp_mem_e5_eval.yaml` (E0 cut to three days, every agent on the no-op maintenance strategy), once per condition and seed. The conditions are the arms: `none`, `policy`, `vault`, `both`, and the faithfulness controls `shuffled` (the policy's lines in another order) and `inverted` (each imperative bullet negated). `--donors richest | all | names:A,B` picks the donor agents (default the richest). Runs land in `runs/exp_mem_e5/<condition>/seed_<n>/`, each with a `transplant.json` manifest; a completed run with the same config and transplant is skipped, anything else already there makes the exit status 2.

```bash
.venv/bin/void transplant --donor-run runs/exp_mem_e0/index_builder/seed_1 --conditions none,both,shuffled --seeds 1-20 --out runs
.venv/bin/python scripts/compare.py runs/exp_mem_e5
```

**E3a, generations** (scripted, free; proposal 6.5): `void generations` chains worlds of `configs/exp_mem_e3a.yaml` (twelve agents, three days, the E5 world with diverse seed policies and no in-world births). After each generation the top half by end balance survive with their notes and policies, and each dead slot holds a child with a survivor's policy: arm `oblique` adds the survivors' most common bullet the parent lacks, arm `vertical` copies it verbatim. Generation `g` of seed `n` is an ordinary run at `runs/exp_mem_e3a/<arm>/seed_<n>/gen_<g>/`; `seed_<n>/generations.json` holds the cohort-by-generation rows and `<arm>/summary.json` the slopes on generation across seeds. `--plant NAME=BULLET` appends a bullet to a founder's policy in generation 0; `--days` shortens the worlds for a smoke run.

```bash
.venv/bin/void generations --arms oblique,vertical --generations 10 --seeds 1-10 --out runs
```

**E4's scripted mirror** (free; proposal 6.6): the E4 design (the five-rung capability ladder crossed with the memory condition) on scripted rungs, so the whole pipeline runs for $0. Arms `default` (no policy, no maintenance), `selfedit` (policy file plus nightly maintenance by the planted `index_builder` strategy) and `flat_tariff` (`selfedit` with output free in-world and one price per input token). A full-length run of one arm takes about a minute. It tests the arms, billing and compare, not the hypothesis: no scripted rung thinks, and scripted agents do not need memory (10.4). The paid arms, `live_mem_e4_*.yaml`, wait for gate G3 (stage S3 of 10.4) and never run under the live Claude budget.

```bash
for arm in default selfedit flat_tariff; do .venv/bin/void run --config configs/exp_mem_e4s_$arm.yaml --seeds 1-5 --tick-seconds 0 --out runs; done
.venv/bin/python scripts/compare.py runs/exp_mem_e4s
```

**The live pilots** (need `GEMINI_API_KEY`; prices in `base.yaml` are list prices to verify before paying; every run is capped by `economy.total_cap_usd`):

| Pilot | Command | Rough cost `[estimate]` |
|---|---|---|
| Scarcity and deception on a live tier (`sharp`), 10 seeds | `for arm in high low; do .venv/bin/void run --config configs/live_scarcity_$arm.yaml --seeds 1-10 --out data/live; done` then `scripts/compare.py data/live/exp_scarcity_live` | about $30 (about $5 on `average`) |
| E1, does self-directed memory help (`average`), 10 seeds x 4 arms | `for arm in a0 a b c; do .venv/bin/void run --config configs/live_mem_e1_$arm.yaml --seeds 1-10 --out data/live; done` then `scripts/compare.py data/live/exp_mem_e1` and `scripts/gates.py runs --g1 data/live/exp_mem_e1` | about $100 |

Read the E1 result in this order (section 6.3 of the proposal): `a` must beat `a0` (memory matters under fog at all), then `c` versus `b` is the question; `c` versus `a` is the effect net of its cost.

## Export and the data card

`void export RUN_DIR` writes one file per table of `world.db` (every table; the per-tick `snapshots` are left out) plus `exam_items_answers` (each exam question joined with its answer) to `RUN_DIR/export/`, or `--out DIR`. `manifest.json` beside them is the data card: run id, config hash, seed, status and schema version; the distinct served model strings of `llm_calls`; the code commit and dirty flag recorded when the run was created; the format; per table its row count, file and columns (declared type and the storage classes present); how the tables join; and what was left out (`run.operator_token_hash` is redacted). The system prompts are in `prompt_texts` and every user turn in `llm_calls.prompt_text`, so any logged call can be rebuilt from the export alone. `void.research.export.read_export(out_dir, table)` reads either format back with the original Python types.

```bash
.venv/bin/void export runs/demo                         # Parquet when pyarrow is importable, else CSV
.venv/bin/void export runs/demo --format csv --out runs/demo/csv
VIRTUAL_ENV=.venv uv pip install -e ".[export]"         # the export extra (pyarrow), needed for Parquet
.venv/bin/void export parquet runs/demo                 # the same as --format parquet; exit 2 with a hint without the extra
```

The CSV is lossless: RFC 4180, UTF-8, a header row, every string (and every BLOB, as hex) quoted, numbers unquoted and NULL an empty unquoted field, so NULL and the empty string stay distinct. The export reads `world.db` in one transaction, so a live run exports a consistent snapshot.

## Live Claude test

Real model calls on the current Haiku, Sonnet and Opus, in five stages with a hard ceiling on total spend. Nothing here runs without a key.

**The key.** Add a workspace-scoped API key as `ANTHROPIC_API_KEY` in the environment's settings (never in chat, never in a file in the repo); a new session picks it up. A user-level key that is not scoped to a workspace also works if `ANTHROPIC_WORKSPACE_ID` is set; it is sent as the `anthropic-workspace-id` header. A key that has been pasted into a chat is exposed: revoke it and make a new one.

**Before any paid stage** (all free, in the Anthropic Console):
- Set a spend limit on the key's workspace of at most $60 minus what that workspace has already been billed, and pass it to the script as `CONSOLE_SPEND_LIMIT_USD` (required). The local guard sees only the runs this repository records; the workspace limit bounds everything the key is billed for.
- Check that the workspace's default inference geo is `global`. The meter prices every call at the list prices; a response that reports another geo, a service tier other than `standard`, or a 1-hour cache write is metered, logged as a `billing:` error, and that tier sends nothing more in the run (verify then fails the run).
- Read the workspace's rate limits for the three models. The `memdep`, `memdep_veins` and `scarcity` stages send about 180 requests a minute to the cheapest model (six agents per tick, about 2 s per call), with about 150k uncached input and 35k output tokens a minute in the memory arm. A 429 or 529 is retried inside the call timeout (at most 4 attempts, after `retry-after`), but if the limits are lower, pace both arms of the stage the same way: `run.tick_seconds` of at least 6 x 60 / (0.7 x the requests-per-minute limit) in `live_claude_dep_base.yaml` and in both `live_claude_scarcity_*.yaml`.

**See the plan first** (no calls, no spend; it lists the caps of the runs still to start, leaving out runs already done or in flight):

```bash
PLAN_ONLY=1 bash scripts/live_claude.sh
```

**Run it:**

```bash
CONSOLE_SPEND_LIMIT_USD=59 bash scripts/live_claude.sh               # all stages, BUDGET_USD=60 by default
STAGES=smoke CONSOLE_SPEND_LIMIT_USD=59 bash scripts/live_claude.sh  # one stage; the smoke must pass before the others start
```

| Stage | Configs | What it shows | Most it can spend |
|---|---|---|---|
| `smoke` | `live_claude_smoke.yaml` | every call purpose on every model works: valid JSON decisions, no 400s, metered cost close to the provider's usage | $1.50 (done: $0.1568) |
| `ladder` | `live_claude_ladder.yaml` | nine agents on three capability rungs, 5 days: the behaviour demo | $10.00 |
| `memdep` | `live_claude_dep_{mem,amnesic}.yaml`, 16 seed pairs | does a real agent use memory well enough to pay for it? (the memory-dependence check of MEMORY_EVOLUTION.md 5.2, the first condition of gate G1; G0 itself stays failed until the scripted E0 passes; the reading is pre-registered in 10.4) | $11.52 |
| `memdep_veins` | `live_claude_dep_veins_{mem,amnesic}.yaml`, 32 pairs on seeds 21-52 (never the tuning seeds 1-10) | the same in the vein world | $23.04 |
| `scarcity` | `live_claude_scarcity_{high,low}.yaml`, 24 pairs, the first 2 a pilot | does scarcity raise deception on a real model? | $14.40 |

The caps are about three times the spend the smoke run's measured usage predicts (about $3.4 for the ladder, $0.12 and $0.07 for a memory and an amnesic run, $0.10 for a scarcity run `[estimate]`). Before the scarcity stage, look at the ladder's swift agents (free): if they almost never `talk` or list claims, the scarcity stage will measure nothing. Its pilot stops the stage when any pilot run listed fewer than `MIN_CLAIMS` (10) claims, since `false_claim_rate` counts only the claims a model lists itself and is undefined without them.

**How spend is counted.** Every run has its own `economy.total_cap_usd`. Real spend is recorded in two places: the run directories under `data/live` (not committed), and the committed spend ledger `docs/research/live_spend.jsonl`, which survives a new machine or a fresh clone. Before each run the script adds up both (per run, the larger of the two) and refuses a run whose cap would take the total past `BUDGET_USD`; it then appends a `reserved` line holding the run's full cap and commits and pushes it to the working branch before the run starts (if the push fails, nothing starts), and appends a `final` line with the real spend when the run ends. Stopped with Ctrl-C, TERM or HUP, the script stops `void run`, waits for it to exit, and records and pushes the `final` line before it exits (a second Ctrl-C does not cut that short). A script killed outright (SIGKILL, a lost machine) leaves the attempt `reserved`, counted at its full cap, until a rerun closes it (below). Ledger lines or commits left off origin are pushed when the script starts, and it does not report success while any remain. `scripts/live_budget.py report --root data/live --ledger docs/research/live_spend.jsonl` lists every run, the open reservations, and where the ledger counts more than the run directories. `void run --budget-usd` and `void serve --budget-usd` count the same records (the repository's `data/live` whatever the working directory, any `--spend-root`, `--out` and the ledger) and refuse a paid run when the ledger is missing. Never edit or drop ledger lines.

**Reruns.** A completed run is verified again with its stage's checks and then skipped; paired stages alternate arms seed by seed, so an interrupted stage still leaves complete pairs. A run that is not usable (failed, interrupted, capped, or completed but not verifying) stops the script with the exact command to move it under `data/live/_rejected/`: inside `data/live` its spend still counts, outside the experiment directories `compare.py` never sees it. Never move a live run out of `data/live`. An attempt a killed script left `reserved` is closed by the next run of the script (`live_budget.py reconcile`) from the directory that attempt's run wrote, once that run has provably ended: its status is not `running`, or the lock its `void run` held under `data/live/.live_locks/` is free. A run that may still be spending is never closed. A run the ledger records but whose directory is gone is refused, and the script lists each attempt: one that finished with real spend (restore the directory, or set `RERUN_LOST=1` to pay for it again; the earlier spend stays counted), or one reserved and never recorded as finished. Close the second kind by hand only when you know it never started (the script was stopped before `void run`, for example while it pushed the reservation) and the workspace's usage in the Console shows no call around its reservation time: `scripts/live_budget.py final --ledger docs/research/live_spend.jsonl --attempt <attempt> --run-dir <its run directory>` records it at $0 (no directory, nothing spent); commit and push the ledger. That appends a line and edits none. Never do this for an attempt another machine may have run; leave it at its cap or use `RERUN_LOST=1`. Compare paired stages with `.venv/bin/python scripts/compare.py data/live/<experiment>`.

**Watch the demo in the 3D viewer.** Instead of the headless `ladder` stage, serve it: `.venv/bin/void serve --config configs/live_claude_ladder.yaml --out data/live --budget-usd 60` and open http://127.0.0.1:8000 (rebuild the web app with `cd web && npm run build` if it is stale). Serving counts the recorded spend but writes no ledger line, so reserve the run first as the script does (`attempt=$(.venv/bin/python scripts/live_budget.py reserve --ledger docs/research/live_spend.jsonl --config configs/live_claude_ladder.yaml --out data/live)`, commit and push the ledger, serve with `VOID_LEDGER_ATTEMPT=$attempt`) and record it afterwards (`scripts/live_budget.py final --ledger docs/research/live_spend.jsonl --attempt $attempt --run-dir data/live/live_claude_ladder`, commit, push). Reopen a finished run with the same command plus `--resume`; it makes no model call. `void mock-feed` and `scripts/analyze.py --replay` refuse a config that could call a paid provider: either would start a new paid simulation whose records are deleted afterwards.

## Tuning the economy

`VoidConfig.warnings()` (printed by the CLI) flags any roster tier whose estimated call cost is outside 0.3 to 2.0 times the forage income per tick, and a total cap that binds before the last day. The levers: `world.forage_yield_usd`, `world.node_regen_per_tick`, `world.scarcity`, `population.starting_balance_usd`, `population.min_reserve_usd`, tier prices, `economy.world_pricing: equalized`.

## Safety switches

- `sandbox.enabled: false` disables gadgets entirely; `sandbox.gate_enabled: false` skips test execution (research question 4) but never the static check or spec validation.
- `economy.daily_cap_usd` and `economy.total_cap_usd` are real-dollar ceilings enforced by reservation before every call.
- `benefactor.enabled`, `epochs`, task posting and approvals are operator actions logged as `operator_command` events.
