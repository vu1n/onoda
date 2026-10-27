---
id: vte-grading
paths:
  - "src/onoda/science/vte.py"
  - "src/onoda/adapters/pi/vte.py"
  - "src/onoda/adapters/pi/vte_contract.py"
  - "src/onoda/adapters/pi/vte_audit.py"
  - "src/onoda/adapters/pi/run.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "pi/extensions/onoda-task.ts"
  - "cases-dev/questdb/**"
  - "experiments/questdb/run.sh"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/adapters/pi/vte.py:evaluate_vte_claims"
  - "src/onoda/adapters/pi/vte.py:vte_report"
  - "src/onoda/adapters/pi/vte_audit.py"
tests:
  - "tests/test_pi_vte_grading.py"
  - "tests/test_pi_vte_end_to_end.py"
  - "tests/test_vte_grader_audit.py"
  - "tests/test_vte_contract.py"
  - "tests/test_pi_vte_faults.py"
---

# VTE claims, identity grading and reports

An agent gets structure and target counts, submits jobs and reports VTE states with cited
job ids. A separate evaluator matches printed identities to protected reference rows and
reports coverage, accuracy, error and process metrics per arm. A mutation audit measures
what the grader still credits when real claims are deliberately made wrong.

## Sub-features

- Agent-only task bundles without reference energies or characters.
- Single-cited-job support, spin/irrep/character identity and SC-NEVPT2 accuracy tolerance.
- Per-arm jobs, executions, failed share, requests and cross-run accurate-count ranges.
- Separate faulted/control arms and known-wrong claim mutation audit.

## How to get to it (user POV)

Use `onoda pi run-vte <case>`, `onoda pi evaluate-vte <run>` with a separate evaluator store,
then `onoda vte report`. `onoda vte audit` uses a throwaway store to mutate claims. The CLI
end-to-end test assembles these inputs offline; the experiment runner is the live counterpart.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Submit and grade a claim, then report it | `uv run pytest tests/test_pi_vte_end_to_end.py::test_cli_run_evaluate_report` | JSON includes identity-based grade and per-arm report from a scripted run. |
| Challenge state identity, units, missing irreps and process denominators | `uv run pytest tests/test_pi_vte_grading.py tests/test_vte_contract.py` | Unsupported claims gain no identity credit; absent evidence and zero/old accuracy denominators stay unresolved. |
| Audit known-wrong claims and injected output | `uv run pytest tests/test_vte_grader_audit.py tests/test_pi_vte_faults.py` | Audit counts surviving mutations and the report keeps injected/no-trigger runs apart from clean arms. |

## Gotchas

- A claim counts only when its one cited job printed it; no cross-job differences or matching
  by energy order. Printed irreps matter. See [grading contract](../decisions/vte-grading-contract.md).
- The 0.3 eV tolerance judges an already identified row; it never identifies a state. Its
  historical draft status and owner rationale are retained in [tolerance context](../decisions/vte-energy-tolerance.md).
- Process metrics are descriptive, not promotion gates. One run cannot measure stability;
  older evaluations without accuracy make relevant aggregates unresolved.
- `label_swap` is measured by the mutation audit, not asserted to fail universally. Coverage
  alone is not accuracy, and equal accurate counts can hide different states.
