#!/usr/bin/env bash
# Clean-workspace demo of the M1 contract (synthetic fixture only; not chemistry):
# execute -> checkpoint -> export -> import elsewhere -> delete original -> replay
# -> reanalyze -> fork before the decision under two policies -> run suffixes
# -> compare lineage and costs -> grade in a protected evaluator store.
# M2 then records authentic ORCA outputs, exports and imports them, and grades
# the ORCA 6.0 TD-DFT case against ORCA 5.0 with the discrepancy left unresolved.
# Every command runs with networking disabled when `unshare -rn` is available.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(cd "$ROOT" && uv run python -c 'import sys; print(sys.executable)')"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

offline() {
  if unshare -rn true 2>/dev/null; then unshare -rn "$@"; else "$@"; fi
}
onoda() { offline "$PY" -m onoda.cli "$@"; }
say() { printf '\n== %s\n' "$*"; }

say "execute the fixture in store-a"
onoda fixture run --run-id demo --store ./store-a | jq -c '{phase, boundary, final}'

say "export (agent-visible) and discard the original store and scheduler"
onoda export demo --store ./store-a --out demo.tar.gz | jq -c '{profile, files}'
rm -rf ./store-a ./store-a.scheduler

say "import into a fresh store and inspect capabilities"
onoda import demo.tar.gz --store ./store-b | jq -c '{roots, objects, records, exact_replay}'
onoda inspect demo --store ./store-b --capabilities | jq -c '.capabilities | map_values(.status)'

say "offline replay"
onoda replay demo --store ./store-b --offline | jq -c '{events, checkpoints: (.checkpoints | length), final_digest, external_calls}'

say "reanalyze the parse step (fixture reanalysis, not scientific replication)"
onoda reanalyze demo --step parse --store ./store-b | jq -c 'map({original, derivation, matches})'
onoda reanalyze demo --step parse --transform fixture-parser@2 --store ./store-b | jq -c 'map({original, matches, differences})'

say "fork at the first pre-decision checkpoint under two policies"
CP="$(onoda checkpoint list demo --store ./store-b | jq -r '[.[] | select(.boundary == "diagnostics-committed")][0].checkpoint')"
for pair in "branch-v1 mock-policy-v1" "branch-v2 mock-policy-v2"; do
  set -- $pair
  sed -e "s/^new_run_id: .*/new_run_id: \"$1\"/" \
      -e "s/^  run_id: null/  run_id: \"demo\"/" \
      -e "s/^  checkpoint_digest: null/  checkpoint_digest: \"$CP\"/" \
      -e "s/^  to_policy: .*/  to_policy: \"$2\"/" \
      "$ROOT/docs/handoff/examples/fork.template.yaml" > "fork-$1.yaml"
  onoda fork demo --checkpoint "$CP" --declaration "fork-$1.yaml" --new-run "$1" --store ./store-b | jq -c '{run_id, phase, boundary}'
  onoda resume "$1" --mode fixture --store ./store-b | jq -c '{run_id, phase, final}'
done

say "compare branches with cost lineage"
onoda compare demo branch-v1 branch-v2 --store ./store-b --include-cost-lineage \
  | jq -c '{comparison_kind, runs: [.runs[] | {run_id, policy, actions, marginal: .costs.marginal.cost_usd, inherited: .costs.inherited_prefix.cost_usd, full_path: .costs.full_path.cost_usd}], physical: .physical_cost.cost_usd}'

say "grade in a separate evaluator store"
for run in branch-v1 branch-v2; do
  onoda evaluate "$run" --store ./store-b --evaluator-store ./evaluator | jq -c '{evaluation_id, assignments}'
done

say "M2: pinned authentic ORCA cases (acquired from cclib, not executed here)"
onoda case list --cases-root "$ROOT/cases" | jq -c '.[] | {case_id, program, review}'

say "record, parse, and export the ORCA 6.0 TD-DFT case; grade it against ORCA 5.0 after import"
onoda case ingest orca6-dvb-tddft --run-id td6 --store ./orca-a --cases-root "$ROOT/cases" \
  | jq -c '{mode, termination, scf, excited_states_status, states, problems}'
onoda export td6 --store ./orca-a --out td6.tar.gz | jq -c '{profile, files}'
onoda import td6.tar.gz --store ./orca-b | jq -c '{roots, objects, records}'
onoda case evaluate td6 --store ./orca-b --evaluator-store ./orca-evaluator \
  --reference-case orca5-dvb-tddft --cases-root "$ROOT/cases" \
  | jq -c '{assignments, unresolved, reason: .metrics["reference_comparison.reason"], first_singlet_diff_au: .metrics["diff.energy_au.m1.r1"], final_energy_diff_eh: .metrics["diff.final_energy"]}'

say "live ORCA execution is off unless an operator enables it"
onoda case ingest orca6-water-mp2 --run-id live --store ./orca-a --execute --cases-root "$ROOT/cases" \
  || echo "(refused as expected)"

say "done (workspace $WORK is removed on exit)"
