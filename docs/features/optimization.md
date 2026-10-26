---
id: optimization
paths:
  - "src/onoda/ablation/**"
  - "src/onoda/optimize/**"
  - "src/onoda/adapters/pi/contract.py"
  - "src/onoda/adapters/pi/seed_candidate.json"
  - "gates/**"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/ablation/run.py"
  - "src/onoda/optimize/loop.py"
  - "src/onoda/optimize/compare.py"
tests:
  - "tests/test_ablation_run.py"
  - "tests/test_ablation_gates.py"
  - "tests/test_optimize_end_to_end.py"
  - "tests/test_optimize_compare.py"
  - "tests/test_live_optimizer.py"
  - "tests/test_optimize_diagnostics.py"
---

# Ablations, harness optimization and promotion

Compare harness variants on the same cases, search allowlisted prompt/tool components with
GEPA, and evaluate a candidate against the seed under explicit capability and efficiency
gates. Noise and bootstrap diagnostics show variability beside a comparison, not a new verdict.

## Sub-features

- Arms × cases × repeats ablation reports, mechanism activation and reducer-loss probes.
- Deterministic exploratory acceptance/final splits, candidate validation and reflection traces.
- Search-wide spend reservation and recorded reflection receipts.
- Whole-round candidate comparisons, zero-worsening capability gates and pinned seed bytes.

## How to get to it (user POV)

Use `onoda ablation run <spec>`, or `onoda optimize seed`, `split`, `run` and `status`.
`onoda optimize noise` compares a candidate to itself before searching; `compare --live`
checks a proposed promotion. The demo and test fixtures exercise the search offline. Live
comparison remains a separately configured operation that spends money.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Run scripted arms and inspect gates | `uv run pytest tests/test_ablation_run.py tests/test_ablation_metrics.py tests/test_ablation_gates.py` | Outcome vectors and reports are stored separately; invalid specs and missing tolerances cannot pass. |
| Propose two candidates on real Pi | `uv run pytest tests/test_optimize_end_to_end.py` | Scripted GEPA search rejects false success and accepts a passing proposal; pending-review splits are labelled exploratory. |
| Check spend, promotion and noise behavior offline | `uv run pytest tests/test_optimize_compare.py tests/test_live_optimizer.py tests/test_optimize_diagnostics.py` | Failed/unknown-cost runs do not promote; budget limits stop before an unaffordable round; intervals remain advisory. |

## Gotchas

- Reflection sees agent-visible traces and verdict/reason classes, never reference values or
  parsed answers. Proposals outside `COMPONENT_VALIDATORS` or leaking acceptance data are invalid.
- Trials use acceptance cases only. The [validation-slice proposal](../proposals/optimizer-validation-slice.md)
  is unratified and not implemented; do not infer a three-way split from that proposal.
- Seed bytes and `SEED_DIGEST` are pinned; even a typo edit creates a new candidate. Promotion
  requires a passing comparison. GEPA pickle state is local scratch, never import/export data.
- Missing capability tolerances are unresolved; false-success tolerance is always zero. Cases
  remain unreviewed, so searches are exploratory and scripted results are synthetic.
- Ablations do not resume under the same id. See [promotion reasons](../decisions/promotion-gates.md)
  and [optimizer scope](../decisions/optimizer-scope.md).
