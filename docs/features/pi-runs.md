---
id: pi-runs
paths:
  - "src/onoda/adapters/pi/contract.py"
  - "src/onoda/adapters/pi/runtime.py"
  - "src/onoda/adapters/pi/run.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "src/onoda/adapters/pi/task.py"
  - "src/onoda/adapters/pi/fork.py"
  - "src/onoda/adapters/pi/sandbox.py"
  - "src/onoda/sandbox_util.py"
  - "pi/extensions/onoda-task.ts"
  - "pi/extensions/onoda-recorder.ts"
  - "pi/extensions/onoda-scripted.ts"
  - "pi/extensions/lib/spool.ts"
  - "pi/package.json"
  - "pi/package-lock.json"
  - "scripts/pi-setup.sh"
  - "src/onoda/cli.py"
entry:
  - "src/onoda/adapters/pi/run.py:run_case"
  - "src/onoda/adapters/pi/runtime.py"
  - "src/onoda/adapters/pi/ingest.py"
tests:
  - "tests/test_pi_end_to_end.py"
  - "tests/test_pi_fork.py"
  - "tests/test_pi_extensions.py"
  - "tests/test_pi_sandbox.py"
  - "tests/test_pi_runtime.py"
---

# Recorded Pi agent runs and replay

Run a pinned Pi agent on a fixed-input case, record exactly what it saw and did, grade its
claims separately, and replay the run under recorded responses. Variants select baseline,
ladder and SoL-Pi mechanisms; forks replay a prefix before continuing from a checkpoint.

## Sub-features

- Isolated launch configuration, explicit live settings and request/cost limits.
- Fsynced/HMAC-chained spools, receipts, claims, compactions and semantic checkpoints.
- Scripted offline provider and recorded-response replay with drift rejection.
- Platform sandbox for live commands; fork validation and mechanism-specific continuation limits.

## How to get to it (user POV)

Install with `scripts/pi-setup.sh`, inspect `onoda pi doctor` and `onoda pi variants`, then use
`onoda pi run`, `onoda pi evaluate`, `onoda pi replay` or `onoda pi fork`. The end-to-end CLI
test creates the script, ladder, stores and paths; `scripts/demo.sh` shows the same sequence.

## Driving it with pytest and TypeScript

| Action | Exact command | Observable result |
|---|---|---|
| Run, grade and replay S0/S3 with the real pinned runtime | `uv run pytest tests/test_pi_end_to_end.py` | Offline scripted runs produce receipts; replay matches; changed context, forged spools and imported tool replay are rejected. |
| Branch and validate extension/tool boundaries | `uv run pytest tests/test_pi_fork.py tests/test_pi_extensions.py tests/test_pi_runtime.py` | Prefix replay and allowed continuations work; invalid variants/settings are refused before execution. |
| Probe command confinement | `uv run pytest tests/test_pi_sandbox.py tests/test_pi_sandbox_seatbelt.py` | Supported platform sandbox blocks host files/network/credentials; unavailable sandbox probes skip. |
| Check extension interfaces | `cd pi && npx tsc -p .` | Installed lockfile types and extension code compile without errors. |

## Gotchas

- Runtime tests skip without pinned Pi. A green skipped run does not verify this feature.
- S1 onward require an explicit ladder. Live runs also require every provider/budget setting
  and a working sandbox; tests use scripted or fake providers, never paid calls.
- The recorder loads last and spools model-visible bytes before handing them over. Its HMAC
  is tamper evidence, not a sandbox against a process that can read the recorder's memory.
- Pi native resume is unimplemented. RLM/imported/finalized runs and some reducer live
  continuations cannot be forked. [Adapter seam](../ADAPTER_SEAM.md) records the runtime gaps.
