---
id: records-and-forks
paths:
  - "src/onoda/store.py"
  - "src/onoda/canonical.py"
  - "src/onoda/records.py"
  - "src/onoda/controller.py"
  - "src/onoda/forks.py"
  - "src/onoda/jobs.py"
  - "src/onoda/budgets.py"
  - "src/onoda/memory.py"
  - "src/onoda/context.py"
  - "src/onoda/fixture.py"
  - "src/onoda/clock.py"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/controller.py:Controller"
  - "src/onoda/store.py:Store"
  - "src/onoda/forks.py"
tests:
  - "tests/test_end_to_end.py:test_clean_workspace_sequence"
  - "tests/test_core.py"
  - "tests/test_forks.py"
  - "tests/test_jobs.py"
  - "tests/test_budgets.py"
  - "tests/test_context.py"
---

# Run records, checkpoints and forks

A caller records a scientific-agent trajectory as immutable objects, receipts and events,
then branches at a semantic checkpoint to compare interventions without inheriting a future
that the branch never observed. The fixture CLI demonstrates this contract with synthetic work.

## Sub-features

- Canonical, float-free records; bytes and records committed before their referencing event.
- Crash-safe mock jobs, budget reservations and separate marginal/inherited cost views.
- Checkpoints, policy/parser/input/memory interventions and dependency invalidation.
- Frozen memory releases, visibility by lineage, observation handles and context compaction.

## How to get to it (user POV)

Start with `onoda fixture run`, list checkpoints with `onoda checkpoint list`, fork with a
filled `docs/handoff/examples/fork.template.yaml`, then `onoda resume` the branch. Use
`onoda compare --include-cost-lineage` to compare branches and `onoda evaluate` to put
judgments in a separate evaluator store. The demo constructs all declarations for you.

## Driving it with pytest and the CLI

| Action | Exact command from the repository root | Observable result |
|---|---|---|
| Run, export, import, branch, resume and compare | `uv run pytest tests/test_end_to_end.py::test_clean_workspace_sequence` | Replay has the original digest and zero external calls; the two policies take different actions and preserve parent history. |
| Exercise storage, crashes, invalidation, memory and budgets | `uv run pytest tests/test_core.py tests/test_controller.py tests/test_jobs.py tests/test_forks.py tests/test_budgets.py tests/test_context.py` | Invalid writes/forks are refused, restart does not double-submit work and lineage restricts memory. |
| Watch the full user journey in fresh temporary stores | `scripts/demo.sh` | Prints fixture branch/cost results, then acquired cases and (with Pi installed) scripted agent experiments; removes its workspace on exit. |

## Gotchas

- There is one authoritative writer per run, not concurrent-writer locking. A `RunView` is a
  snapshot: rebuild it after appending events.
- Fork only at a journaled, quiescent checkpoint. Resume is semantic restart, not restoration
  of native process or Python state. Case/Pi runs do not support the fixture resume command.
- Fixture work and costs are synthetic, not chemistry or provider spend. Unknown cost is
  never silently zero. See [record integrity](../decisions/record-integrity.md).
- Memory from a sibling that saw beyond the branch cut is rejected (`test_forks.py`), even
  when that sibling's lesson sounds useful.
