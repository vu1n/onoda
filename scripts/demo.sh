#!/usr/bin/env bash
# Clean-workspace demo of the M1 contract (synthetic fixture only; not chemistry):
# execute -> checkpoint -> export -> import elsewhere -> delete original -> replay
# -> reanalyze -> fork before the decision under two policies -> run suffixes
# -> compare lineage and costs -> grade in a protected evaluator store.
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
labrun() { offline "$PY" -m labrun.cli "$@"; }
say() { printf '\n== %s\n' "$*"; }

say "execute the fixture in store-a"
labrun fixture run --run-id demo --store ./store-a | jq -c '{phase, boundary, final}'

say "export (agent-visible) and discard the original store and scheduler"
labrun export demo --store ./store-a --out demo.tar.gz | jq -c '{profile, files}'
rm -rf ./store-a ./store-a.scheduler

say "import into a fresh store and inspect capabilities"
labrun import demo.tar.gz --store ./store-b | jq -c '{roots, objects, records, exact_replay}'
labrun inspect demo --store ./store-b --capabilities | jq -c '.capabilities | map_values(.status)'

say "offline replay"
labrun replay demo --store ./store-b --offline | jq -c '{events, checkpoints: (.checkpoints | length), final_digest, external_calls}'

say "reanalyze the parse step (fixture reanalysis, not scientific replication)"
labrun reanalyze demo --step parse --store ./store-b | jq -c 'map({original, derivation, matches})'
labrun reanalyze demo --step parse --transform fixture-parser@2 --store ./store-b | jq -c 'map({original, matches, differences})'

say "fork at the first pre-decision checkpoint under two policies"
CP="$(labrun checkpoint list demo --store ./store-b | jq -r '[.[] | select(.boundary == "diagnostics-committed")][0].checkpoint')"
for pair in "branch-v1 mock-policy-v1" "branch-v2 mock-policy-v2"; do
  set -- $pair
  sed -e "s/^new_run_id: .*/new_run_id: \"$1\"/" \
      -e "s/^  run_id: null/  run_id: \"demo\"/" \
      -e "s/^  checkpoint_digest: null/  checkpoint_digest: \"$CP\"/" \
      -e "s/^  to_policy: .*/  to_policy: \"$2\"/" \
      "$ROOT/docs/handoff/examples/fork.template.yaml" > "fork-$1.yaml"
  labrun fork demo --checkpoint "$CP" --declaration "fork-$1.yaml" --new-run "$1" --store ./store-b | jq -c '{run_id, phase, boundary}'
  labrun resume "$1" --mode fixture --store ./store-b | jq -c '{run_id, phase, final}'
done

say "compare branches with cost lineage"
labrun compare demo branch-v1 branch-v2 --store ./store-b --include-cost-lineage \
  | jq -c '{comparison_kind, runs: [.runs[] | {run_id, policy, actions, marginal: .costs.marginal.cost_usd, inherited: .costs.inherited_prefix.cost_usd, full_path: .costs.full_path.cost_usd}], physical: .physical_cost.cost_usd}'

say "grade in a separate evaluator store"
for run in branch-v1 branch-v2; do
  labrun evaluate "$run" --store ./store-b --evaluator-store ./evaluator | jq -c '{evaluation_id, assignments}'
done

say "done (workspace $WORK is removed on exit)"
