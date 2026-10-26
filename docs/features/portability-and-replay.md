---
id: portability-and-replay
paths:
  - "src/onoda/portability.py"
  - "src/onoda/replay.py"
  - "src/onoda/decisions.py"
  - "src/onoda/evaluation.py"
  - "src/onoda/store.py"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/portability.py:export_bundle"
  - "src/onoda/portability.py:import_bundle"
  - "src/onoda/decisions.py:RecordedDecisionProvider"
tests:
  - "tests/test_portability.py"
  - "tests/test_replay.py"
  - "tests/test_decisions.py"
  - "tests/test_evaluation.py"
  - "tests/test_end_to_end.py"
---

# Portable bundles and exact offline replay

Export a run's reachable records, move the bundle to another directory or machine, inspect
which operations it supports, and replay recorded decisions offline. Reanalysis adds a new
derivation instead of rewriting the original observation or evaluation.

## Sub-features

- Deterministic directory/archive exports, agent-visible and evaluator profiles, explicit omissions.
- Data-only import with digest, schema, path, expansion and dependency checks.
- Capability reporting for thin/redacted bundles; no automatic fetching of missing bytes.
- Exact request matching, pure-step reanalysis and separate evaluator derivations.

## How to get to it (user POV)

Record a run, then use `onoda export`, `onoda import`, `onoda inspect --capabilities` and
`onoda replay --offline`. The fixture walkthrough in `scripts/demo.sh` deletes its original
store before importing, so it proves that the second store stands alone.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Move a bundle to another absolute path | `uv run pytest tests/test_portability.py -k 'round_trip or archive_bytes or thin_bundle'` | Full bundles round-trip; archive bytes are deterministic; thin bundles report blocked capabilities without fetching. |
| Try corrupt or unsafe bundles and exported secrets | `uv run pytest tests/test_portability.py` | Digest corruption, unsafe paths, oversized input, journal extension and credentials are rejected. |
| Replay and reanalyze a recorded trajectory | `uv run pytest tests/test_replay.py tests/test_decisions.py tests/test_evaluation.py tests/test_end_to_end.py` | Replay matches exact requests, drift raises `ReplayMismatch`, and evaluations stay in the evaluator store. |

## Gotchas

- An import is data only: no installs, network, pickle or bundled code execution. Pi tool replay
  refuses imported runs because it would execute tool calls; generic record inspection remains usable.
- Agent-visible exports exclude evaluator references and secrets; redacted exports are derivatives,
  not claims of exact replay. See [evaluator isolation](../decisions/evaluator-isolation.md).
- No nearest-match or live fallback exists for decisions. A request digest mismatch fails closed.
- The product's stored provenance strings (including `doc://`) are data. Their meaning is preserved
  by the [legacy identifier lookup](../decisions/README.md#legacy-identifiers).
