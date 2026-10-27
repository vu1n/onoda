---
id: orca-broker
paths:
  - "src/onoda/science/orca_broker.py"
  - "src/onoda/science/orca_sandbox.py"
  - "src/onoda/science/orca_faults.py"
  - "src/onoda/science/orca_checks.py"
  - "src/onoda/science/orca_summary.py"
  - "src/onoda/adapters/pi/run.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "src/onoda/adapters/pi/vte_contract.py"
  - "pi/extensions/onoda-task.ts"
  - "pi/extensions/lib/orca-input.ts"
  - "src/onoda/sandbox_util.py"
entry:
  - "src/onoda/science/orca_broker.py:OrcaBroker"
  - "src/onoda/science/orca_sandbox.py"
  - "src/onoda/science/orca_faults.py:load_fault_plan"
tests:
  - "tests/test_orca_broker.py"
  - "tests/test_orca_sandbox.py"
  - "tests/test_orca_faults.py"
  - "tests/test_pi_vte_end_to_end.py"
  - "tests/test_pi_vte_faults.py"
  - "tests/test_orca_summary.py"
  - "tests/test_orca_checks.py"
---

# Host ORCA broker, confinement and injected faults

A VTE agent submits its own ORCA input through a host broker and receives a journaled job,
parser summary and advisory checks. The broker owns real execution, caching and budgets.
An optional, pinned fault plan tests recovery while retaining both actual and served output.

## Sub-features

- Unix-socket request/reply protocol, input validation, per-job confinement and execution cap.
- Cache receipts, rereading/listing jobs, parser-derived summary and ladder advice.
- Recorded replay with zero solver executions and spool/journal agreement checks.
- One-shot `killed-before-nevpt2` fault injection, transient retry and hidden provenance.

## How to get to it (user POV)

`onoda pi run-vte` stages the socket and tools (`orca_submit`, `orca_output`, `orca_jobs`).
`--fault-plan` opts into a specific pinned plan; ordinary runs have no injection. The tests
create labelled stand-in executables and temporary socket paths, so these journeys need
neither an ORCA license nor a live model.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Submit, cache, read and replay jobs | `uv run pytest tests/test_orca_broker.py tests/test_pi_vte_end_to_end.py` | One stand-in execution and a cache hit are recorded; replay serves recorded output with zero executions. |
| Verify confinement and summaries | `uv run pytest tests/test_orca_sandbox.py tests/test_orca_summary.py tests/test_orca_checks.py` | Unsafe job inputs are rejected and summaries/advice derive from printed data; sandbox-only probes may skip. |
| Inject a failure, retry and replay it | `uv run pytest tests/test_orca_faults.py tests/test_pi_vte_faults.py` | First eligible job is faulted once, later retry is normal, hidden plan/real output stay out of agent views and faulted forks are refused. |

## Gotchas

- Use a short work directory: AF_UNIX socket paths are limited (104 bytes on macOS).
- The broker, not agent tools, owns the only `max_executions`; refused submissions do not
  count. Real ORCA requires a sandbox that passes its trial run.
- Parser normal termination, not the process exit code, decides success. Advisory checks and
  summaries never grade claims. See [broker boundary](../decisions/orca-broker-boundary.md).
- Faults apply only to eligible real executions; a genuine failure consumes the trigger
  without injection. Faulted/edited-plan/not-fired arms stay distinct in reports. Tests remain
  synthetic because they use scripted Pi and stand-ins; no live pilot is implied.
