#!/usr/bin/env bash
# QUESTDB slice: Baseline (S0, no ladder) vs Decision (S1, the reconstructed
# ladder) over the eight dev-only QUESTDB cases (A62), REPEATS runs each, with
# a live model in the sandbox and ORCA 6.1.1 executed on this host through the
# broker (A59). Then grades every run by state identity (A58) and prints the
# per-arm report (coverage, MAE, false successes, common-target MAE).
#
# Written for Vu's Mac (ORCA at ~/code/onoda/orca-6.1.1, OpenMPI at
# ~/code/onoda/orca-openmpi). Nothing is defaulted on: the model, its
# credential, the request and cost caps, and ORCA execution itself must each be
# given explicitly, or the script stops before anything runs.
#
#   ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=<model> \
#   CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=60 MAX_COST_USD=2.00 \
#     experiments/questdb/run.sh
#
# Optional: REPEATS (2), CASES (all eight), ORCA_HOME, MPI_HOME, WORK (the short
# work root, /tmp/qv: the broker socket and OpenMPI's sockets in each job dir
# must stay under 104 bytes on macOS), OUT (stores, cache and reports;
# $WORK/out), ORCA_WORK (ORCA job dirs; $WORK/jobs), DOCS_DIR (*.md docs given
# to both arms; the ORCA manual is not redistributable, so none by default),
# MEMORY_DIR (memory-*.md tiers given to both arms, A75; default
# experiments/questdb/memory, `none` for no memory),
# MAX_EXECUTIONS (24), MAX_WALL_SECONDS (1800 per ORCA job), TIMEOUT (3600 per
# run; one agent shell command is capped separately, A72), REPORT_REMINDER (off;
# 1 tells an agent that stops without report_vtes, once, to report), ONLY_RUNS (run
# ids such as "S1-thioacetone-r1", space-separated: run only those, e.g. to rerun
# one run into a fresh WORK), PI_MODELS (a Pi models.json copied into each run's
# agent dir, for a model the pinned Pi catalog lacks; its sha256 is recorded in the
# run's config), FAULT_PLAN (off; a fault plan file, broker-fault-injection: every
# case, arm and repeat then runs twice, clean as `<arm>-<case>-r<n>` and faulted as
# `<arm>-fault-<case>-r<n>`, each from a fresh empty ORCA cache of its own, in an
# order that alternates arms and conditions, and the report keeps every arm).
# Every ORCA job runs
# in the job sandbox (A65), which sees ORCA_HOME, MPI_HOME and its own job dir
# only, so none of these may sit inside ORCA_HOME or MPI_HOME, and nothing here
# is written inside the repository.
#
# Progress: one line per finished run goes to $OUT/progress.log (wall time,
# coverage, false successes). A run whose `run-vte` fails is logged there and
# skipped, so one bad run does not stop the experiment; its stderr is kept in
# $OUT/<run>.run.err. Rerunning with the same OUT resumes: a run that already
# has its evaluate JSON is not run again. PARALLEL (1) runs that many runs at
# once; above 1 each run gets its own agent store, evaluator store and ORCA
# cache under $OUT/runs/<run>/ (a store takes one writer at a time), its own
# ORCA job root $ORCA_WORK/<run> (so no two brokers share scheduler state), and the
# report pools them. Each ORCA job may use up to 4 processes (the broker's
# cap), so PARALLEL=4 fills 16 cores.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
need() { [ -n "${!1:-}" ] || { echo "run.sh: $1 must be set explicitly (no default)" >&2; exit 2; }; }
for v in PROVIDER MODEL CREDENTIAL_ENV MAX_REQUESTS MAX_COST_USD; do need "$v"; done
# no live run without ENABLE_LIVE_EXECUTION=1 and every provider setting
if [ "${ENABLE_LIVE_EXECUTION:-}" != 1 ]; then
  echo "run.sh: ORCA execution is off; set ENABLE_LIVE_EXECUTION=1 to run ORCA on this host" >&2
  exit 2
fi

ORCA_HOME="${ORCA_HOME:-$HOME/code/onoda/orca-6.1.1}"
MPI_HOME="${MPI_HOME:-$HOME/code/onoda/orca-openmpi}"
WORK="${WORK:-/tmp/qv}"
OUT="${OUT:-$WORK/out}"
ORCA_WORK="${ORCA_WORK:-$WORK/jobs}"
REPEATS="${REPEATS:-2}"
CASES="${CASES:-questdb-acetaldehyde questdb-formamide questdb-ketene questdb-diazomethane questdb-acetone questdb-thioacetone questdb-methylenecyclopropene questdb-cyclopropenone}"
LADDER="$ROOT/experiments/questdb/ladder-from-paper.md"
[ -x "$ORCA_HOME/orca" ] || { echo "run.sh: no ORCA at $ORCA_HOME/orca" >&2; exit 2; }
[ -x "$MPI_HOME/bin/mpirun" ] || { echo "run.sh: no mpirun at $MPI_HOME/bin" >&2; exit 2; }

DOCS=()
if [ -n "${DOCS_DIR:-}" ]; then
  for d in "$DOCS_DIR"/*.md; do DOCS+=(--doc "$d"); done
fi
MEMORY_DIR="${MEMORY_DIR:-$ROOT/experiments/questdb/memory}"
if [ "$MEMORY_DIR" != none ]; then  # frozen per run: staged and pinned like any doc (A75)
  for d in "$MEMORY_DIR"/memory-*.md; do
    [ -f "$d" ] || { echo "run.sh: no memory-*.md in $MEMORY_DIR (MEMORY_DIR=none for none)" >&2; exit 2; }
    DOCS+=(--doc "$d")
  done
fi

onoda() { (cd "$ROOT" && uv run onoda "$@"); }
graded() {  # run JSON, evaluate JSON -> "<status>: found N of M, false successes K"
  python3 - "$1" "$2" <<'PY'
import json, sys
r, e = (json.load(open(p)) for p in sys.argv[1:3])
end = r["status"] if r["complete"] else "INCOMPLETE (%s), a harness failure, not a result" % r["status"]
print("%s: found %s of %s, false successes %s"
      % (end, e["coverage"], e["coverage_total"], e["false_success_claims"]))
PY
}
progress() {  # run id, start (SECONDS), what happened
  local line
  line="$(date -u +%H:%MZ) $1 $(( (SECONDS - $2) / 60 )) min: $3"
  echo "$line" | tee -a "$OUT/progress.log" >&2
}
mkdir -p "$OUT" "$WORK" "$ORCA_WORK"
PARALLEL="${PARALLEL:-1}"
case "$PARALLEL" in ''|*[!0-9]*|0) echo "run.sh: PARALLEL must be a positive integer" >&2; exit 2;; esac
if [ -n "${FAULT_PLAN:-}" ]; then
  [ -f "$FAULT_PLAN" ] || { echo "run.sh: no fault plan at $FAULT_PLAN" >&2; exit 2; }
  FAULT_PLAN="$(cd "$(dirname "$FAULT_PLAN")" && pwd)/$(basename "$FAULT_PLAN")"
fi
store_args() {  # run id -> the run's agent store, evaluator store and ORCA cache
  if [ "$PARALLEL" = 1 ] && [ -n "${FAULT_PLAN:-}" ]; then
    # pilot runs start from a fresh empty cache, no preseed
    echo "$OUT/store $OUT/evaluator $OUT/orca-caches/$1"
  elif [ "$PARALLEL" = 1 ]; then
    echo "$OUT/store $OUT/evaluator $OUT/orca-cache"
  else  # a store takes one writer at a time, so parallel runs get one each
    echo "$OUT/runs/$1/store $OUT/runs/$1/evaluator $OUT/runs/$1/orca-cache"
  fi
}
run_id_of() {  # case id, arm, repeat, condition (clean|fault)
  if [ "$4" = fault ]; then echo "$2-fault-${1#questdb-}-r$3"; else echo "$2-${1#questdb-}-r$3"; fi
}
run_one() {  # case id, arm, repeat, condition (clean|fault)
  local case_id=$1 arm=$2 r=$3 cond=${4:-clean} run_id jobs store evaluator cache started ladder=() reminder=() models=() fault=()
  run_id="$(run_id_of "$case_id" "$arm" "$r" "$cond")"
  read -r store evaluator cache <<< "$(store_args "$run_id")"
  jobs="$ORCA_WORK"
  # Parallel runs each get their own ORCA job root, so no two brokers share scheduler state.
  [ "$PARALLEL" = 1 ] || jobs="$ORCA_WORK/$run_id"
  [ "$arm" = S1 ] && ladder=(--ladder "$LADDER")
  [ "${REPORT_REMINDER:-0}" = 1 ] && reminder=(--report-reminder)
  [ -n "${PI_MODELS:-}" ] && models=(--pi-models "$PI_MODELS")
  [ "$cond" = fault ] && fault=(--fault-plan "$FAULT_PLAN")
  echo "== $run_id" >&2
  started=$SECONDS
  mkdir -p "$(dirname "$store")"
  if ! onoda pi run-vte "$case_id" --variant "$arm" --run-id "$run_id" \
    --store "$store" --work-dir "$WORK/$run_id" \
    --live --provider "$PROVIDER" --model "$MODEL" --credential-env "$CREDENTIAL_ENV" \
    --max-requests "$MAX_REQUESTS" --max-cost-usd "$MAX_COST_USD" \
    --enable-live-execution --orca-binary "$ORCA_HOME/orca" \
    --orca-work-root "$jobs" --orca-cache "$cache" \
    --orca-mpi-home "$MPI_HOME" \
    --max-wall-seconds "${MAX_WALL_SECONDS:-1800}" \
    --max-executions "${MAX_EXECUTIONS:-24}" --timeout "${TIMEOUT:-3600}" \
    ${DOCS[@]+"${DOCS[@]}"} ${ladder[@]+"${ladder[@]}"} ${reminder[@]+"${reminder[@]}"} \
    ${models[@]+"${models[@]}"} ${fault[@]+"${fault[@]}"} \
    > "$OUT/$run_id.run.json" 2> "$OUT/$run_id.run.err"; then
    progress "$run_id" "$started" "run failed, skipped (see $run_id.run.err)"
    return 0
  fi
  onoda pi evaluate-vte "$run_id" --store "$store" \
    --evaluator-store "$evaluator" > "$OUT/$run_id.evaluate.json"
  progress "$run_id" "$started" "$(graded "$OUT/$run_id.run.json" "$OUT/$run_id.evaluate.json")"
}
PLANNED=()  # "case arm repeat condition", in run order
if [ -z "${FAULT_PLAN:-}" ]; then
  for case_id in $CASES; do
    for arm in S0 S1; do
      for r in $(seq 1 "$REPEATS"); do PLANNED+=("$case_id $arm $r clean"); done
    done
  done
else  # repeat by repeat, so the first block holds one run per cell; arms and conditions alternate
  for r in $(seq 1 "$REPEATS"); do
    for case_id in $CASES; do
      if [ $((r % 2)) = 1 ]; then order="S0:clean S1:fault S1:clean S0:fault"
      else order="S1:clean S0:fault S0:clean S1:fault"; fi
      for cell in $order; do PLANNED+=("$case_id ${cell%%:*} $r ${cell##*:}"); done
    done
  done
fi
ALL=()
for planned in "${PLANNED[@]}"; do
  read -r case_id arm r cond <<< "$planned"
  run_id="$(run_id_of "$case_id" "$arm" "$r" "$cond")"
  if [ -n "${ONLY_RUNS:-}" ]; then
    case " $ONLY_RUNS " in *" $run_id "*) ;; *) continue;; esac
  fi
  ALL+=("$run_id")
  [ -s "$OUT/$run_id.evaluate.json" ] && continue  # resume: graded by an earlier invocation
  if [ "$PARALLEL" = 1 ]; then
    run_one "$case_id" "$arm" "$r" "$cond"
  else  # bash 3.2 (macOS) has no `wait -n`, so poll the running jobs
    while [ "$(jobs -rp | wc -l | tr -d " ")" -ge "$PARALLEL" ]; do sleep 5; done
    run_one "$case_id" "$arm" "$r" "$cond" &
  fi
done
wait
RUNS=()
for run_id in "${ALL[@]}"; do
  [ -s "$OUT/$run_id.evaluate.json" ] && RUNS+=("$run_id")
done
[ ${#RUNS[@]} -gt 0 ] || { echo "run.sh: no run was graded" >&2; exit 1; }
ARMS=(--arm S0 --arm S1)
[ -n "${FAULT_PLAN:-}" ] && ARMS=()  # faulted arms are keyed by plan, beside their clean controls
if [ "$PARALLEL" = 1 ]; then
  onoda vte report "${RUNS[@]}" --store "$OUT/store" --evaluator-store "$OUT/evaluator" \
    ${ARMS[@]+"${ARMS[@]}"} | tee "$OUT/report.json"
else
  onoda vte report "${RUNS[@]}" --run-stores "$OUT/runs" ${ARMS[@]+"${ARMS[@]}"} | tee "$OUT/report.json"
fi
