---
id: scientific-cases
paths:
  - "src/onoda/science/contract.py"
  - "src/onoda/science/runner.py"
  - "src/onoda/science/evaluator.py"
  - "src/onoda/science/orca_parser.py"
  - "src/onoda/science/orca_evidence.py"
  - "src/onoda/science/orca_local.py"
  - "cases/**"
  - "scripts/questdb_cases.py"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/science/runner.py"
  - "src/onoda/science/orca_parser.py:parse_orca_output"
  - "src/onoda/science/orca_evidence.py:build_packet"
tests:
  - "tests/test_smoke_cases.py"
  - "tests/test_orca_parser.py"
  - "tests/test_orca_parser_nevpt2.py"
  - "tests/test_science_evaluator.py"
  - "tests/test_orca_evidence.py"
  - "tests/test_orca_local.py"
---

# Pinned scientific cases, parsing and evidence

Inspect and ingest pinned authentic ORCA outputs, parse the printed values deterministically,
and compare states by identity. Evidence packets attach source byte locations to parser facts
so another reader can check a result against the exact log rather than trusting a summary.

## Sub-features

- Case manifests and SHA-256 verification, acquisition receipts and explicit execution provenance.
- ORCA 5/6 parsing, including multireference/NEVPT2 outputs and honest partial observations.
- Identity-based comparisons with declared tolerances and unresolved results when evidence is absent.
- Evidence packet generation/checking with source digest, full evidence inventory and bounded display.

## How to get to it (user POV)

Use `onoda case list`, then `onoda case ingest <case>` and `onoda case evaluate <run>` with
separate run/evaluator stores. Ingest defaults to acquired output. `onoda vte evidence` and
`onoda vte evidence-check` build and verify evidence packets; their CLI tests provide concrete
file arguments. `--execute` is a separate, explicitly configured live mode.

## Driving it with pytest and the CLI

| Action | Exact command | Observable result |
|---|---|---|
| List the shipped cases | `uv run onoda case list` | JSON identifies pinned cases and their review status. |
| Ingest authentic fixtures and compare their parsed states | `uv run pytest tests/test_smoke_cases.py tests/test_orca_parser.py tests/test_orca_parser_nevpt2.py tests/test_science_evaluator.py` | Acquired receipts retain provenance; printed values parse; unsupported identity/tolerance stays unresolved. |
| Build and corrupt an evidence packet | `uv run pytest tests/test_orca_evidence.py` | Byte/line locations match the source; forged facts and bad budgets are refused; display truncation preserves the full inventory. |
| Exercise optional execution without real ORCA | `uv run pytest tests/test_orca_local.py` | Labelled stand-ins test live-setting refusal, timeout and recorded execution behavior. |

## Gotchas

- Never edit vendored case bytes: manifests pin them. `cases-dev/` data is separate from the
  redistributable cases and optimizer splits; see [provenance](../decisions/case-data-provenance.md).
- An acquired output was not executed here. A zero exit code does not prove normal termination;
  only the deterministic parser determines what the log supports.
- Never pair states by nearest energy. Missing multiplicity or a justified comparison tolerance
  yields unresolved, not pass. The cases are still pending human scientific review.
- Evidence packets prove source fidelity, not scientific readiness or wavefunction/job binding.
  Their `interpretations` are empty; a compact display can omit lines without losing inventory.
