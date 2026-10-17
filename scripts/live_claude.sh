#!/usr/bin/env bash
# The live Claude test (docs/RUNBOOK.md, "Live Claude test"): five stages in order, every run guarded by the
# cumulative live budget. Real model calls cost real money; this script is the sanctioned way to make them.
#
#   1 smoke         configs/live_claude_smoke.yaml: 3 agents, 2 days x 3 ticks, every call purpose on every tier;
#                   must pass `live_budget.py verify --strict` before any other stage may run
#   2 ladder        configs/live_claude_ladder.yaml: 9 agents on three rungs, 5 days x 12 ticks, one run
#   3 memdep        configs/live_claude_dep_{mem,amnesic}.yaml: the memory-dependence check, DEP_SEEDS pairs
#   4 memdep_veins  configs/live_claude_dep_veins_{mem,amnesic}.yaml: the same in the vein world, VEIN_SEEDS pairs
#   5 scarcity      configs/live_claude_scarcity_{high,low}.yaml: the scarcity-and-deception pilot; the first
#                   SCARCITY_PILOT pairs run first and the rest only if every pilot run listed MIN_CLAIMS claims
#
# Spend is counted from two records: the run directories under OUT (and the repository's data/live), and the
# committed spend ledger (LEDGER), which survives a new machine or a fresh clone. Before every run the script adds
# up both and refuses the run when that plus its economy.total_cap_usd would exceed BUDGET_USD; it then appends a
# `reserved` line (the run's full cap) to the ledger and commits and pushes it BEFORE the run starts (no paid call
# starts until its reservation is off this machine), and a `final` line with the real spend after it ends, whatever
# happened. `void run --budget-usd` checks the same again. A completed run is verified again and then skipped, so
# the script can be re-run after an interruption; a run that is not usable must be moved under OUT/_rejected/
# (never out of OUT). The script stops at the first run that fails, is refused, or does not verify, and prints the
# real spend of every stage.
#
# Preconditions for a paid run: ANTHROPIC_API_KEY in the environment (never in a file or a chat), and a spend limit
# on that key's workspace in the Anthropic Console, stated as CONSOLE_SPEND_LIMIT_USD (at most BUDGET_USD): the
# local guard sees only these runs, the workspace limit bounds everything that key is billed for.
#
# Environment: ANTHROPIC_API_KEY, CONSOLE_SPEND_LIMIT_USD (both required to run), ANTHROPIC_WORKSPACE_ID (optional,
# sent as the anthropic-workspace-id header), BUDGET_USD (default 60), OUT (default data/live; this checkout's
# data/live, REPO_LIVE, is counted as well), STAGES (default
# "smoke ladder memdep memdep_veins scarcity"), DEP_SEEDS (default 1..16), VEIN_SEEDS (default 21..52, never the
# vein tuning seeds 1-10), SCARCITY_SEEDS (default 1..24), SCARCITY_PILOT (default 2 pairs), MIN_CLAIMS (default 10),
# LEDGER (default docs/research/live_spend.jsonl), LEDGER_PUSH (default 1: commit and push every ledger line to
# LEDGER_BRANCH, default claude/trusting-goodall-ew0p38; 0 keeps it on this machine), RERUN_LOST=1 (pay again for a
# run the ledger records but whose directory is gone), PLAN_ONLY=1 (print the plan and the budget arithmetic, run
# nothing).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
VOID="${VOID:-$ROOT/.venv/bin/void}"
OUT="${OUT:-$ROOT/data/live}"
REPO_LIVE="${REPO_LIVE:-$ROOT/data/live}"  # always counted as well, so an OUT override cannot hide earlier spend
BUDGET_USD="${BUDGET_USD:-60}"
STAGES="${STAGES:-smoke ladder memdep memdep_veins scarcity}"
DEP_SEEDS="${DEP_SEEDS:-$(seq -s ' ' 1 16)}"
VEIN_SEEDS="${VEIN_SEEDS:-$(seq -s ' ' 21 52)}"
SCARCITY_SEEDS="${SCARCITY_SEEDS:-$(seq -s ' ' 1 24)}"
SCARCITY_PILOT="${SCARCITY_PILOT:-2}"
MIN_CLAIMS="${MIN_CLAIMS:-10}"
PLAN_ONLY="${PLAN_ONLY:-0}"
LEDGER="${LEDGER:-$ROOT/docs/research/live_spend.jsonl}"
LEDGER_PUSH="${LEDGER_PUSH:-1}"
LEDGER_BRANCH="${LEDGER_BRANCH:-claude/trusting-goodall-ew0p38}"
RERUN_LOST="${RERUN_LOST:-0}"
CONSOLE_SPEND_LIMIT_USD="${CONSOLE_SPEND_LIMIT_USD:-}"
HELPER="$ROOT/scripts/live_budget.py"
CONFIGS="$ROOT/configs"
SMOKE="$CONFIGS/live_claude_smoke.yaml"

die() {
  echo "live_claude: $*" >&2
  exit 1
}

helper() { "$PY" "$HELPER" "$@"; }

spent() { helper spent --root "$OUT" --root "$REPO_LIVE" --ledger "$LEDGER" --plain; }

usd_sub() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.4f", a - b }'; }

# the run directory of a config (and seed, for the paired stages) without starting anything
run_dir() {
  if [[ -n "${2:-}" ]]; then helper rundir --config "$1" --out "$OUT" --seed "$2"; else helper rundir --config "$1" --out "$OUT"; fi
}

# where an unusable run goes: inside OUT its spend still counts, outside the experiment directories compare.py
# never sees it
quarantine_hint() {
  local rel="${1#"$OUT"/}"
  echo "move it under $OUT/_rejected/ (mkdir -p $OUT/_rejected && mv $1 $OUT/_rejected/$(date +%Y%m%dT%H%M%S)-${rel//\//_}); never move a live run out of $OUT"
}

# commit and push the ledger, so the line just written is off this machine before anything else happens
ledger_commit() {
  if [[ "$LEDGER_PUSH" == "0" ]]; then
    echo "live_claude: WARNING: LEDGER_PUSH=0: the ledger line ($1) stays on this machine only" >&2
    return 0
  fi
  local branch
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)" || return 1
  if [[ "$branch" != "$LEDGER_BRANCH" ]]; then
    echo "live_claude: the checkout is on '$branch', not $LEDGER_BRANCH, where the ledger is pushed" >&2
    return 1
  fi
  git -C "$ROOT" add -- "$LEDGER" && git -C "$ROOT" commit -q -m "live spend: $1" -- "$LEDGER" \
    && git -C "$ROOT" push -q origin "$branch"
}

# run_one CONFIG SEED [verify flags...]: guard, reserve, run, verify, record (SEED is "" for a single run); a
# completed run is verified again with the same flags and skipped
run_one() {
  local config="$1" seed="$2"
  shift 2
  local dir status label attempt rc=0 vrc=0 verified=""
  local seed_args=()
  if [[ -n "$seed" ]]; then seed_args=(--seed "$seed"); fi
  label="$(basename "$config")${seed:+ seed $seed}"
  dir="$(run_dir "$config" "$seed")"
  status="$(helper status "$dir")"
  if [[ "$status" == "completed" ]]; then
    helper verify "$dir" "$@" >/dev/null || die "$label completed but does not verify: $(quarantine_hint "$dir"), then rerun to redo it"
    echo "live_claude: skip $label: already completed and verified ($dir)"
    return 0
  fi
  if [[ "$status" != "absent" ]]; then
    die "$dir exists with status '$status': look at it (void inspect --run-dir $dir --money --events 20), then $(quarantine_hint "$dir")"
  fi
  if ! helper lost --ledger "$LEDGER" --config "$config" "${seed_args[@]}" --root "$OUT" --root "$REPO_LIVE"; then
    [[ "$RERUN_LOST" == "1" ]] || die "$label was paid for before (see $LEDGER) but its directory is gone: restore it under $OUT, or set RERUN_LOST=1 to pay for it again (the earlier spend stays counted)"
  fi
  helper check --root "$OUT" --root "$REPO_LIVE" --ledger "$LEDGER" --budget "$BUDGET_USD" --config "$config" \
    "${seed_args[@]}" || die "budget guard refused $label"
  attempt="$(helper reserve --ledger "$LEDGER" --config "$config" "${seed_args[@]}" --out "$OUT")" || die "could not write $LEDGER"
  if ! ledger_commit "reserve $attempt"; then
    helper final --ledger "$LEDGER" --attempt "$attempt" --run-dir "$dir" >/dev/null || true  # nothing started: $0
    die "the reservation of $label is not off this machine (commit or push failed); nothing was started: fix that, then rerun"
  fi
  local args=(run --config "$config" --out "$OUT" --budget-usd "$BUDGET_USD")
  if [[ -n "$seed" ]]; then args+=(--seeds "$seed"); fi
  VOID_LEDGER="$LEDGER" VOID_LEDGER_ATTEMPT="$attempt" "$VOID" "${args[@]}" || rc=$?
  if [[ "$rc" == "0" ]]; then helper verify "$dir" "$@" || vrc=$?; fi
  if [[ "$rc" == "0" && "$vrc" == "0" ]]; then verified="--verified"; fi
  helper final --ledger "$LEDGER" --attempt "$attempt" --run-dir "$dir" $verified \
    || die "could not record the end of $label in $LEDGER (it stays counted at its cap)"
  ledger_commit "final $attempt" || die "the final ledger line of $label was not pushed (the run stays counted at its cap until it is)"
  [[ "$rc" == "0" ]] || die "run did not complete: $label ($dir); $(quarantine_hint "$dir")"
  [[ "$vrc" == "0" ]] || die "run is not usable: $dir; $(quarantine_hint "$dir")"
}

# the smoke run passes when it is on disk and verifies strictly, or (a fresh clone) the ledger records it verified
smoke_passed() {
  local dir status
  dir="$(run_dir "$SMOKE")"
  status="$(helper status "$dir")"
  if [[ "$status" == "completed" ]]; then
    helper verify "$dir" --strict >/dev/null 2>&1
  elif [[ "$status" == "absent" ]]; then
    helper passed --ledger "$LEDGER" --config "$SMOKE"
  else
    return 1
  fi
}

stage_smoke() {
  if smoke_passed; then
    echo "live_claude: skip smoke: it passed already"
    return 0
  fi
  run_one "$SMOKE" "" --strict --expect-purposes decide,maintain,exam,chronicle
}

stage_ladder() {
  run_one "$CONFIGS/live_claude_ladder.yaml" "" --max-error-rate 0.05 --expect-purposes decide,maintain,exam
}

# paired stages alternate the arms seed by seed, so an interrupted stage still leaves complete pairs; a run in which
# a real-dollar cap blocked a call is not usable (the arms would no longer be treated alike)
stage_pairs() {
  local experiment="$1" first="$2" second="$3" seeds="$4" s
  for s in $seeds; do
    run_one "$first" "$s" --max-error-rate 0.05 --no-cap-blocks
    run_one "$second" "$s" --max-error-rate 0.05 --no-cap-blocks
  done
  "$PY" "$ROOT/scripts/compare.py" "$OUT/$experiment" || die "compare refused $OUT/$experiment"
}

stage_memdep() {
  stage_pairs exp_mem_dep_claude "$CONFIGS/live_claude_dep_mem.yaml" "$CONFIGS/live_claude_dep_amnesic.yaml" "$DEP_SEEDS"
}

stage_memdep_veins() {
  stage_pairs exp_mem_dep_veins_claude "$CONFIGS/live_claude_dep_veins_mem.yaml" "$CONFIGS/live_claude_dep_veins_amnesic.yaml" "$VEIN_SEEDS"
}

# false_claim_rate is undefined in a run whose agents list no claim, so a short pilot must show claims first
stage_scarcity() {
  local high="$CONFIGS/live_claude_scarcity_high.yaml" low="$CONFIGS/live_claude_scarcity_low.yaml" s pilot
  local dirs=()
  pilot="$(awk -v n="$SCARCITY_PILOT" '{ for (i = 1; i <= n && i <= NF; i++) printf "%s ", $i }' <<<"$SCARCITY_SEEDS")"
  if [[ -n "${pilot// /}" ]]; then
    for s in $pilot; do
      run_one "$high" "$s" --max-error-rate 0.05 --no-cap-blocks
      run_one "$low" "$s" --max-error-rate 0.05 --no-cap-blocks
      dirs+=("$(run_dir "$high" "$s")" "$(run_dir "$low" "$s")")
    done
    helper claims "${dirs[@]}" --min "$MIN_CLAIMS" \
      || die "scarcity pilot: a run listed fewer than $MIN_CLAIMS claims, so false_claim_rate would be undefined or rest on a handful; the other pairs were not run (MIN_CLAIMS=0 runs them anyway)"
  fi
  stage_pairs exp_scarcity_claude "$high" "$low" "$SCARCITY_SEEDS"
}

# the caps of every run a stage would start (the most it can spend), for the plan printout
stage_caps() {
  local n
  case "$1" in
    smoke)
      if smoke_passed; then echo "0.00"; else helper cap --config "$SMOKE"; fi ;;
    ladder) helper cap --config "$CONFIGS/live_claude_ladder.yaml" ;;
    memdep)
      n="$(wc -w <<<"$DEP_SEEDS")"
      awk -v n="$n" -v c="$(helper cap --config "$CONFIGS/live_claude_dep_mem.yaml")" 'BEGIN { printf "%.2f", 2 * n * c }' ;;
    memdep_veins)
      n="$(wc -w <<<"$VEIN_SEEDS")"
      awk -v n="$n" -v c="$(helper cap --config "$CONFIGS/live_claude_dep_veins_mem.yaml")" 'BEGIN { printf "%.2f", 2 * n * c }' ;;
    scarcity)
      n="$(wc -w <<<"$SCARCITY_SEEDS")"
      awk -v n="$n" -v c="$(helper cap --config "$CONFIGS/live_claude_scarcity_high.yaml")" 'BEGIN { printf "%.2f", 2 * n * c }' ;;
    *) die "unknown stage '$1' (stages: smoke ladder memdep memdep_veins scarcity)" ;;
  esac
}

cd "$ROOT"
[[ -x "$PY" ]] || die "no Python at $PY (create the venv first)"
[[ -f "$LEDGER" ]] || die "no spend ledger at $LEDGER: it is the record of real spend that survives a new machine; restore it from git (never start it afresh)"
for stage in $STAGES; do stage_caps "$stage" >/dev/null; done  # unknown stage names fail before anything runs

already="$(spent)"
total_caps=0
echo "live_claude: budget \$$BUDGET_USD, already spent \$$already (run directories under $OUT and the ledger $LEDGER)"
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
[[ -n "$CONSOLE_SPEND_LIMIT_USD" ]] || die "set CONSOLE_SPEND_LIMIT_USD to the spend limit set on the key's workspace in the Anthropic Console (at most BUDGET_USD, \$$BUDGET_USD): this script sees only its own runs, the workspace limit bounds everything the key is billed for"
awk -v l="$CONSOLE_SPEND_LIMIT_USD" -v b="$BUDGET_USD" 'BEGIN { exit !(l + 0 > 0 && l + 0 <= b + 0) }' \
  || die "CONSOLE_SPEND_LIMIT_USD=$CONSOLE_SPEND_LIMIT_USD must be above 0 and at most BUDGET_USD=$BUDGET_USD"
echo "live_claude: the workspace spend limit is stated as \$$CONSOLE_SPEND_LIMIT_USD"

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

echo "live_claude: all stages done; real spend per stage: ${summary[*]}; total \$$(spent) of \$$BUDGET_USD"
