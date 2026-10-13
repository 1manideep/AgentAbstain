#!/usr/bin/env bash
# The live Claude test (docs/RUNBOOK.md, "Live Claude test"): four stages in order, every run guarded by the
# cumulative live budget. Real model calls cost real money; this script is the sanctioned way to make them.
#
#   1 smoke     configs/live_claude_smoke.yaml: 3 agents, 2 days x 3 ticks, every call purpose on every tier;
#               must pass `live_budget.py verify --strict` before any other stage may run
#   2 ladder    configs/live_claude_ladder.yaml: 9 agents on three rungs, 5 days x 12 ticks, one run
#   3 memdep    configs/live_claude_dep_{mem,amnesic}.yaml: the memory-dependence check, DEP_SEEDS pairs
#   4 scarcity  configs/live_claude_scarcity_{high,low}.yaml: the scarcity-and-deception pilot, SCARCITY_SEEDS pairs
#
# Before every run the real spend already recorded under OUT is added up (scripts/live_budget.py) and the run is
# refused when that plus its economy.total_cap_usd would exceed BUDGET_USD; `void run --budget-usd` checks the same
# again. A run that already completed is skipped, so the script can be re-run after an interruption. The script
# stops at the first run that fails, is refused, or does not verify, and prints the real spend of every stage.
#
# Environment: ANTHROPIC_API_KEY (required; set it in the environment, never in a file or a chat),
# ANTHROPIC_WORKSPACE_ID (optional, sent as the anthropic-workspace-id header), BUDGET_USD (default 60),
# OUT (default data/live), STAGES (default "smoke ladder memdep scarcity"), DEP_SEEDS (default 1..16),
# SCARCITY_SEEDS (default 1..12), PLAN_ONLY=1 (print the plan and the budget arithmetic, run nothing).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
VOID="${VOID:-$ROOT/.venv/bin/void}"
OUT="${OUT:-$ROOT/data/live}"
BUDGET_USD="${BUDGET_USD:-60}"
STAGES="${STAGES:-smoke ladder memdep scarcity}"
DEP_SEEDS="${DEP_SEEDS:-$(seq -s ' ' 1 16)}"
SCARCITY_SEEDS="${SCARCITY_SEEDS:-$(seq -s ' ' 1 12)}"
PLAN_ONLY="${PLAN_ONLY:-0}"
HELPER="$ROOT/scripts/live_budget.py"
CONFIGS="$ROOT/configs"
SMOKE="$CONFIGS/live_claude_smoke.yaml"

die() {
  echo "live_claude: $*" >&2
  exit 1
}

helper() { "$PY" "$HELPER" "$@"; }

spent() { helper spent --root "$OUT" --plain; }

usd_sub() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.4f", a - b }'; }

# the run directory of a config (and seed, for the paired stages) without starting anything
run_dir() {
  if [[ -n "${2:-}" ]]; then helper rundir --config "$1" --out "$OUT" --seed "$2"; else helper rundir --config "$1" --out "$OUT"; fi
}

# run_one CONFIG SEED [verify flags...]: guard, run, verify (SEED is "" for a single run); skips a completed run
run_one() {
  local config="$1" seed="$2"
  shift 2
  local dir status label
  label="$(basename "$config")${seed:+ seed $seed}"
  dir="$(run_dir "$config" "$seed")"
  status="$(helper status "$dir")"
  if [[ "$status" == "completed" ]]; then
    echo "live_claude: skip $label: already completed ($dir)"
    return 0
  fi
  if [[ "$status" != "absent" ]]; then
    die "$dir exists with status '$status': look at it (void inspect --run-dir $dir --money --events 20), then move it away"
  fi
  helper check --root "$OUT" --budget "$BUDGET_USD" --config "$config" || die "budget guard refused $label"
  local args=(run --config "$config" --out "$OUT" --budget-usd "$BUDGET_USD")
  if [[ -n "$seed" ]]; then args+=(--seeds "$seed"); fi
  "$VOID" "${args[@]}" || die "run did not complete: $label ($dir)"
  helper verify "$dir" "$@" || die "run is not usable: $dir"
}

smoke_passed() {
  local dir
  dir="$(run_dir "$SMOKE")"
  [[ "$(helper status "$dir")" == "completed" ]] && helper verify "$dir" --strict >/dev/null 2>&1
}

stage_smoke() {
  run_one "$SMOKE" "" --strict --expect-purposes decide,maintain,exam,chronicle
}

stage_ladder() {
  run_one "$CONFIGS/live_claude_ladder.yaml" "" --max-error-rate 0.05 --expect-purposes decide,maintain,exam
}

# paired stages alternate the arms seed by seed, so an interrupted stage still leaves complete pairs
stage_pairs() {
  local experiment="$1" first="$2" second="$3" seeds="$4" s
  for s in $seeds; do
    run_one "$first" "$s" --max-error-rate 0.05
    run_one "$second" "$s" --max-error-rate 0.05
  done
  "$PY" "$ROOT/scripts/compare.py" "$OUT/$experiment" || die "compare refused $OUT/$experiment"
}

stage_memdep() {
  stage_pairs exp_mem_dep_claude "$CONFIGS/live_claude_dep_mem.yaml" "$CONFIGS/live_claude_dep_amnesic.yaml" "$DEP_SEEDS"
}

stage_scarcity() {
  stage_pairs exp_scarcity_claude "$CONFIGS/live_claude_scarcity_high.yaml" "$CONFIGS/live_claude_scarcity_low.yaml" "$SCARCITY_SEEDS"
}

# the caps of every run a stage would start (the most it can spend), for the plan printout
stage_caps() {
  local n
  case "$1" in
    smoke) helper cap --config "$SMOKE" ;;
    ladder) helper cap --config "$CONFIGS/live_claude_ladder.yaml" ;;
    memdep)
      n="$(wc -w <<<"$DEP_SEEDS")"
      awk -v n="$n" -v c="$(helper cap --config "$CONFIGS/live_claude_dep_mem.yaml")" 'BEGIN { printf "%.2f", 2 * n * c }' ;;
    scarcity)
      n="$(wc -w <<<"$SCARCITY_SEEDS")"
      awk -v n="$n" -v c="$(helper cap --config "$CONFIGS/live_claude_scarcity_high.yaml")" 'BEGIN { printf "%.2f", 2 * n * c }' ;;
    *) die "unknown stage '$1' (stages: smoke ladder memdep scarcity)" ;;
  esac
}

cd "$ROOT"
[[ -x "$PY" ]] || die "no Python at $PY (create the venv first)"
for stage in $STAGES; do stage_caps "$stage" >/dev/null; done  # unknown stage names fail before anything runs

already="$(spent)"
total_caps=0
echo "live_claude: budget \$$BUDGET_USD, already spent \$$already under $OUT"
for stage in $STAGES; do
  caps="$(stage_caps "$stage")"
  total_caps="$(awk -v a="$total_caps" -v b="$caps" 'BEGIN { printf "%.2f", a + b }')"
  echo "live_claude:   stage $stage: caps up to \$$caps"
done
echo "live_claude: the planned stages can spend at most \$$total_caps (each run is checked again before it starts)"
if [[ "$PLAN_ONLY" == "1" ]]; then
  exit 0
fi

# presence only: the key is never printed, logged or written anywhere
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "ANTHROPIC_API_KEY is not set: export it in this shell's environment (never in a file or a chat)"

summary=()
for stage in $STAGES; do
  if [[ "$stage" != "smoke" ]] && ! smoke_passed; then
    die "stage $stage needs a completed smoke run that passes 'live_budget.py verify --strict' first (STAGES=smoke)"
  fi
  before="$(spent)"
  echo "live_claude: === stage $stage (spent so far \$$before) ==="
  "stage_$stage"
  after="$(spent)"
  summary+=("$stage \$$(usd_sub "$after" "$before")")
  echo "live_claude: === stage $stage done: real spend \$$(usd_sub "$after" "$before") (total \$$after) ==="
done

echo "live_claude: all stages done; real spend per stage: ${summary[*]}; total under $OUT \$$(spent) of \$$BUDGET_USD"
