# brief backfill — context map

Repo: `/home/user/onoda`

## Summary

- docs: 35 (adr 3, agent-guide 2, doc 19, plan 5, readme 6)
- decision-genre docs (adr/design/spec/contract): 3
- ⚠ untracked decisions: 2 · decisions missing a status field: 1
- code signals: 11 (refs 0, context 2, rationale 9)
- source modules: 60

## Decision-genre docs (primary backfill inputs)

| doc | genre | status field | tracked | title |
|-----|-------|--------------|---------|-------|
| `.claude/skills/brief-author-decision/SKILL.md` | adr | yes | **NO** | <one line> |
| `.claude/skills/brief-backfill/SKILL.md` | adr | **no** | **NO** | brief backfill — reconstruct the decision layer |
| `docs/ADAPTER_SEAM.md` | adr | yes | yes | Adapter seam: Pi-first, harness-neutral |

## Other docs (NOT decisions — plans/reviews/runbooks/readmes)

Per genre-split: these are snapshots/guides, not governed decisions. Do not import as decisions.

- `.claude/skills/brief-amend/SKILL.md` — doc ⚠ untracked
- `.claude/skills/brief-feature-map/SKILL.md` — doc ⚠ untracked
- `.claude/skills/brief-review/SKILL.md` — doc ⚠ untracked
- `CLAUDE.md` — agent-guide
- `PLAN.md` — plan
- `README.md` — readme
- `cases-dev/orca/cclib-data/README.md` — readme
- `cases-dev/questdb/README.md` — readme
- `docs/ASSUMPTIONS.md` — doc
- `docs/handoff/ACCEPTANCE_TESTS.md` — doc
- `docs/handoff/AGENTS.md` — agent-guide
- `docs/handoff/BUILD_SPEC.md` — doc
- `docs/handoff/CODEX_KICKOFF.md` — doc
- `docs/handoff/EXPERIMENT_PLAN.md` — doc
- `docs/handoff/HANDOFF_README.md` — doc
- `docs/handoff/PORTABLE_RUN_SPEC.md` — doc
- `docs/handoff/SOL_PI_DELTA.md` — doc
- `docs/handoff/SOURCE_NOTES.md` — doc
- `docs/plans/M0-M1-PLAN.md` — plan
- `docs/plans/M2-PLAN.md` — plan
- `docs/plans/M3-PLAN.md` — plan
- `docs/plans/M3b-PLAN.md` — plan
- `experiments/ablation-live-rlm/ladder.md` — doc
- `experiments/ablation-live/ladder.md` — doc
- `experiments/orca-exec/RESULTS.md` — doc
- `experiments/questdb-phase0/README.md` — readme
- `experiments/questdb-phase0/questdb-raw/LICENSE.md` — doc
- `experiments/questdb/ladder-from-paper.md` — doc
- `experiments/tb4-slice/README.md` — readme
- `experiments/tb4-slice/RESULTS.md` — doc
- `tests/ablation_fixtures/ladder.md` — doc
- `tests/fixtures/orca611-nevpt2/README.md` — readme

## Decision signals in code (candidate decisions / existing refs)


**`pi/extensions/lib/rlm-protocol.ts`**
- L93 [context] `* rlm: {prompt: string, context: string | null}. commit_state: {name, json}:`

**`src/onoda/adapters/pi/contract.py`**
- L445 [context] `context: Sha256  # raw JSON bytes of the context / provider payload`

**`experiments/questdb/run.sh`**
- L32 [rationale] `# has its evaluate JSON is not run again. Runs are serial because the agent`

**`pi/extensions/onoda-rlm.ts`**
- L246 [rationale] `// several threads must not let two children read the same remaining budget.`

**`src/onoda/adapters/pi/contract.py`**
- L824 [rationale] `# may hold no other braces (no `{{`, fields, or format specs), and it must not`

**`src/onoda/adapters/pi/sandbox.py`**
- L165 [rationale] `# The wrappers are the inner boundary: Pi must not rewrite them.`

**`src/onoda/adapters/tb/container.py`**
- L171 [rationale] `# A `.pi/` the agent writes into its workspace must not reconfigure Pi or SoL-Pi.`

**`src/onoda/science/orca_local.py`**
- L314 [rationale] `# helper, a stray MPI rank) must not outlive it, nor touch`

**`tests/test_ablation_gates.py`**
- L468 [rationale] `# A run cut off by its budget shows fewer bytes only because it stopped early.`

**`tests/test_orca_local.py`**
- L289 [rationale] `# Resubmitting under the SAME key must not silently re-run it either.`

**`tests/test_pi_sandbox_seatbelt.py`**
- L192 [rationale] `# Pi is unconfined on macOS, so its file tools must not reach the wrappers:`

## Source modules (for related_code scoping)

`experiments/orca-exec`, `experiments/questdb`, `experiments/questdb-phase0`, `pi/extensions`, `pi/rlm`, `scripts/demo.sh`, `scripts/pi-setup.sh`, `scripts/questdb_cases.py`, `src/onoda`, `tests/conftest.py`, `tests/pi_fixtures`, `tests/test_ablation_gates.py`, `tests/test_ablation_metrics.py`, `tests/test_ablation_run.py`, `tests/test_budgets.py`, `tests/test_context.py`, `tests/test_controller.py`, `tests/test_core.py`, `tests/test_decisions.py`, `tests/test_dev_cases.py`, `tests/test_end_to_end.py`, `tests/test_evaluation.py`, `tests/test_forks.py`, `tests/test_gate_files.py`, `tests/test_jobs.py`, `tests/test_live_optimizer.py`, `tests/test_optimize.py`, `tests/test_optimize_compare.py`, `tests/test_optimize_end_to_end.py`, `tests/test_orca_broker.py`, `tests/test_orca_checks.py`, `tests/test_orca_local.py`, `tests/test_orca_parser.py`, `tests/test_orca_parser_nevpt2.py`, `tests/test_orca_sandbox.py`, `tests/test_pi_end_to_end.py`, `tests/test_pi_extensions.py`, `tests/test_pi_ingest.py`, `tests/test_pi_ingest_vte.py`, `tests/test_pi_rlm_extension.py`, `tests/test_pi_rlm_ingest.py`, `tests/test_pi_runtime.py`, `tests/test_pi_sandbox.py`, `tests/test_pi_sandbox_seatbelt.py`, `tests/test_pi_task.py`, `tests/test_pi_vte_end_to_end.py`, `tests/test_pi_vte_grading.py`, `tests/test_pi_vte_tools.py`, `tests/test_portability.py`, `tests/test_questdb_cases.py`, `tests/test_replay.py`, `tests/test_rlm_contract.py`, `tests/test_rlm_end_to_end.py`, `tests/test_rlm_kernel.py`, `tests/test_science_evaluator.py`, `tests/test_smoke_cases.py`, `tests/test_sources.py`, `tests/test_systemone.py`, `tests/test_tb_slice.py`, `tests/test_vte_contract.py`
