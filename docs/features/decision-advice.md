---
id: decision-advice
paths:
  - "src/onoda/systemone.py"
  - "src/onoda/decision_tune.py"
  - "src/onoda/adapters/pi/vte_shadow.py"
  - "src/onoda/adapters/pi/vte_memory_shadow.py"
  - "experiments/questdb/memory/**"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/adapters/pi/vte_shadow.py:shadow_run"
  - "src/onoda/decision_tune.py:tune"
  - "src/onoda/adapters/pi/vte_memory_shadow.py"
tests:
  - "tests/test_systemone.py"
  - "tests/test_vte_shadow_labels.py"
  - "tests/test_decision_tune.py"
  - "tests/test_questdb_memory.py"
  - "tests/test_pi_vte_end_to_end.py"
---

# Shadow decisions, prompt tuning and memory tiers

Record how a System One decision model would judge each broker job beside the scientific
grade, then tune question wording against labelled examples. Frozen Markdown memory tiers
are staged symmetrically for experiment arms; a separate memory shadow compares recall
against those tiers without changing an agent's run.

## Sub-features

- Jev/Clef clients, excerpts and advisory rule answers with probability/cost receipts.
- Grader-labelled shadow datasets and GEPA search over question instructions only.
- Frozen memory tier documents, no reference answers, and kypp recall shadow metrics.

## How to get to it (user POV)

`onoda vte shadow-questions` exposes the template; `onoda vte shadow` records answers and
`onoda vte shadow-dataset` creates labels for `onoda decide tune`. `onoda vte memory-shadow`
records the recall comparison. The end-to-end tests provide fake models/recall and a recorded
job; no external service or credentials are needed for the verification commands below.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Call fake decision transports and derive labels | `uv run pytest tests/test_systemone.py tests/test_vte_shadow_labels.py` | Live defaults refuse unset settings, malformed answers error and grader labels retain their meaning. |
| Tune wording with a scripted objective | `uv run pytest tests/test_decision_tune.py` | Search changes instructions only, respects call caps and records deterministic dataset/split behavior. |
| Record shadow advice next to a real scripted run | `uv run pytest tests/test_pi_vte_end_to_end.py tests/test_questdb_memory.py` | Shadow answers stay separate from grades, memory recall is scored and tier documents exclude reference values. |

## Gotchas

- Neither decision-model answers nor deterministic ladder checks grade chemistry. Shadow data
  belongs in the evaluator store; the running agent never sees those answers.
- Tuning changes wording only, not criteria, labels, thresholds, parser or evaluator. The
  [tuning document](../decisions/decision-prompt-tuning.md) retains its draft status.
- Memory tiers are frozen plain Markdown and identical for both arms; no reference energies
  or reference active spaces belong in them. These experiment tiers are separate from core
  lineage-restricted memory releases in [records and forks](records-and-forks.md).
