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
# Optional: REPEATS (2), CASES (all eight), ORCA_HOME, MPI_HOME, OUT (where the
# stores and cache go), WORK (the Pi work root; keep it short: the broker
# socket path must stay under 104 bytes on macOS), DOCS_DIR (*.md docs given to
# both arms; the ORCA manual is not redistributable, so none by default),
# MAX_EXECUTIONS (24), MAX_WALL_SECONDS (1800 per ORCA job), TIMEOUT (7200 per run).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
need() { [ -n "${!1:-}" ] || { echo "run.sh: $1 must be set explicitly (no default)" >&2; exit 2; }; }
for v in PROVIDER MODEL CREDENTIAL_ENV MAX_REQUESTS MAX_COST_USD; do need "$v"; done
if [ "${ENABLE_LIVE_EXECUTION:-}" != 1 ]; then
  echo "run.sh: ORCA execution is off; set ENABLE_LIVE_EXECUTION=1 to run ORCA on this host" >&2
  exit 2
fi

ORCA_HOME="${ORCA_HOME:-$HOME/code/onoda/orca-6.1.1}"
MPI_HOME="${MPI_HOME:-$HOME/code/onoda/orca-openmpi}"
OUT="${OUT:-$ROOT/questdb-out}"
WORK="${WORK:-/tmp/qv}"
REPEATS="${REPEATS:-2}"
CASES="${CASES:-questdb-acetaldehyde questdb-formamide questdb-ketene questdb-diazomethane questdb-acetone questdb-thioacetone questdb-methylenecyclopropene questdb-cyclopropenone}"
LADDER="$ROOT/experiments/questdb/ladder-from-paper.md"
[ -x "$ORCA_HOME/orca" ] || { echo "run.sh: no ORCA at $ORCA_HOME/orca" >&2; exit 2; }
export PATH="$MPI_HOME/bin:$PATH"

DOCS=()
if [ -n "${DOCS_DIR:-}" ]; then
  for d in "$DOCS_DIR"/*.md; do DOCS+=(--doc "$d"); done
fi

onoda() { (cd "$ROOT" && uv run onoda "$@"); }
mkdir -p "$OUT" "$WORK"
RUNS=()
for case_id in $CASES; do
  for arm in S0 S1; do
    ladder=()
    [ "$arm" = S1 ] && ladder=(--ladder "$LADDER")
    for r in $(seq 1 "$REPEATS"); do
      run_id="$arm-${case_id#questdb-}-r$r"
      echo "== $run_id" >&2
      onoda pi run-vte "$case_id" --variant "$arm" --run-id "$run_id" \
        --store "$OUT/store" --work-dir "$WORK/$run_id" \
        --live --provider "$PROVIDER" --model "$MODEL" --credential-env "$CREDENTIAL_ENV" \
        --max-requests "$MAX_REQUESTS" --max-cost-usd "$MAX_COST_USD" \
        --enable-live-execution --orca-binary "$ORCA_HOME/orca" \
        --orca-work-root "$OUT/orca-jobs" --orca-cache "$OUT/orca-cache" \
        --max-wall-seconds "${MAX_WALL_SECONDS:-1800}" \
        --max-executions "${MAX_EXECUTIONS:-24}" --timeout "${TIMEOUT:-7200}" \
        ${DOCS[@]+"${DOCS[@]}"} ${ladder[@]+"${ladder[@]}"} > "$OUT/$run_id.run.json"
      onoda pi evaluate-vte "$run_id" --store "$OUT/store" \
        --evaluator-store "$OUT/evaluator" > "$OUT/$run_id.evaluate.json"
      RUNS+=("$run_id")
    done
  done
done

onoda vte report "${RUNS[@]}" --store "$OUT/store" --evaluator-store "$OUT/evaluator" \
  --arm S0 --arm S1 | tee "$OUT/report.json"
