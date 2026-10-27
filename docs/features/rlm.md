---
id: rlm
paths:
  - "pi/extensions/onoda-rlm.ts"
  - "pi/extensions/lib/rlm-protocol.ts"
  - "pi/rlm/onoda_rlm_kernel.py"
  - "src/onoda/adapters/pi/contract.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "src/onoda/adapters/pi/run.py"
  - "src/onoda/adapters/pi/sandbox.py"
entry:
  - "pi/extensions/onoda-rlm.ts"
  - "pi/rlm/onoda_rlm_kernel.py"
  - "src/onoda/adapters/pi/ingest.py"
tests:
  - "tests/test_rlm_kernel.py"
  - "tests/test_pi_rlm_extension.py"
  - "tests/test_pi_rlm_ingest.py"
  - "tests/test_rlm_end_to_end.py"
---

# Recursive Pi tools and committed kernel state

The S1r harness gives an agent a Python REPL and bounded recursive child-agent calls. It
records each child as its own run, charges requests/costs to the parent and commits explicit
JSON state so the record describes what the agent relied on.

## Sub-features

- Persistent `ipython` cells, host tool calls and bounded output.
- `rlm()` children with depth/count/request limits and ingested child provenance.
- `commit_state(name, value)` for JSON state and replay of the parent/child tree.

## How to get to it (user POV)

Select S1r for a fixed-case Pi run and provide a ladder/candidate. The agent uses `ipython`,
`rlm` and `commit_state`. `scripts/demo.sh` supplies a synthetic script; the RLM end-to-end
test is the shortest reproducible child-run example.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Execute cells and commit state | `uv run pytest tests/test_rlm_kernel.py` | Kernel protocol returns bounded output and committed JSON; invalid requests are rejected. |
| Spawn a child, ingest the tree and replay | `uv run pytest tests/test_pi_rlm_extension.py tests/test_pi_rlm_ingest.py tests/test_rlm_end_to_end.py` | Child has its own run/receipts, parent pays, exact replay succeeds and a changed child prompt fails closed. |

## Gotchas

- Uncommitted Python memory is not portable state. Native kernel restart from committed state
  is not implemented; pickle is never a record format.
- Depth/child/budget refusal occurs before spawning. A child must be ingested before the
  parent cites it, and shares the parent's spend/request allowance.
- Helper import validation is a lint, not confinement; the kernel uses the command sandbox
  when enabled. VTE tasks and Pi forks refuse RLM variants in the current implementation.
- The onoda RLM is built around semantic checkpoints; no DSPy runtime is required.
  See [dependency rationale](../decisions/dependency-policy.md).
