# onoda

Portable, forkable run records for scientific-agent experiments. An experiment
belongs to its data and provenance, not to the agent framework that ran it:
a run can be exported, imported on another machine, inspected offline,
partially recomputed from raw bytes, and branched from an explicit checkpoint
without losing lineage or cost accounting.

This is milestone **M0 + M1** of the handoff in [`docs/handoff/`](docs/handoff/)
(v0.2; [`SOL_PI_DELTA.md`](docs/handoff/SOL_PI_DELTA.md) overrides conflicting
text, so Pi is the first agent adapter). Everything runs on a **completely
synthetic fixture**. Nothing here is a solver result or a chemistry claim, and
no chemistry experiment has been reproduced.

## Quick start

```sh
uv sync
uv run pytest          # all suites are offline; no credentials, network, or solver
scripts/demo.sh        # the full clean-workspace sequence, network disabled
```

The demo executes the fixture, exports it, deletes the original store, imports
into a fresh directory, replays offline, reanalyzes the parse step, forks at
the pre-decision checkpoint under two policies, runs both suffixes, compares
lineage and costs, and grades both branches in a separate evaluator store.

## CLI

`labrun` implements the interface proposed in PORTABLE_RUN_SPEC §14. Every
command prints JSON.

```sh
labrun fixture run --run-id demo --store ./store-a
labrun checkpoint list demo --store ./store-a
labrun export demo --store ./store-a --profile agent-visible --out demo.tar.gz
labrun import demo.tar.gz --store ./store-b
labrun inspect demo --store ./store-b --capabilities
labrun replay demo --store ./store-b --offline
labrun reanalyze demo --step parse --store ./store-b [--transform fixture-parser@2]
labrun fork demo --checkpoint <digest> --declaration fork.yaml --new-run demo-branch --store ./store-b
labrun resume demo-branch --mode fixture --store ./store-b
labrun compare demo demo-branch --store ./store-b --include-cost-lineage
labrun evaluate demo-branch --store ./store-b --evaluator-store ./evaluator
```

The mock scheduler stands in for an external system, so its state lives beside
the store (`<store>.scheduler/`) and is never exported.

## Layout

| Module | Responsibility |
|---|---|
| `canonical` | RFC 8785 canonical JSON via the `rfc8785` library; floats rejected; SHA-256 digests |
| `records` | Versioned frozen schemas: artifact, task, step, invocation, event, checkpoint, run, fork, evaluation; migration registry |
| `store` | Content-addressed objects, immutable records, hash-chained per-run journals, crash recovery, disposable SQLite index |
| `controller` | Phase machine over five checkpoint boundaries; idempotent against the journal; semantic restart |
| `jobs` | Mock external scheduler with idempotent submission and reconciliation |
| `budgets` | Reservations, run caps, experiment quotas, marginal / inherited / full-path / physical cost views |
| `decisions` | Decision requests, mock policies, exact-request offline replay |
| `fixture` | Synthetic programs, versioned parsers, mock policies, wavefunction-bound mappings |
| `forks` | Fork declarations, invalidation, visibility cut, `RunView` |
| `memory` | Lessons with provenance, frozen releases, filtered retrieval |
| `replay` | Offline inspection replay and deterministic reanalysis |
| `portability` | Export/import bundles, visibility profiles, thin and redacted bundles, capability inspection |
| `evaluation` | Protected references and graders in a separate evaluator store |
| `context` | SoL-Pi mechanisms as recorded fixtures: ObservationPack, compaction, reducer, action fusion |
| `adapters.agents` | The `AgentAdapter` seam for Pi (and Prime), with a deterministic mock |

## Capability matrix

What was actually demonstrated, and on what. "Fixture" means the synthetic
workflow in `labrun.fixture`; none of these rows is a scientific result.

| Capability (PORTABLE_RUN_SPEC §1) | Status | Evidence |
|---|---|---|
| Inspect / replay | Demonstrated on fixture, offline, after import into a fresh store | T01, T09; `tests/test_replay.py`, `tests/test_end_to_end.py` |
| Reanalyze | Demonstrated for the pure parse step; new derivation receipts, originals untouched | T02; `tests/test_replay.py` |
| Reexecute | Fixture programs only, via the mock scheduler. No solver or model reexecution exists | `inspect --capabilities` |
| Resume | Semantic restart from checkpoints; native session resume refused | T10, T14; `tests/test_jobs.py`, `tests/test_controller.py` |
| Fork | Quiescent checkpoints; policy, parser, input, calc-config and memory interventions with invalidation | T03, T05, T07; `tests/test_forks.py` |
| Repeat experiment | Not implemented in M1 | — |

## Acceptance tests

| Test | Where |
|---|---|
| T01 clean-workspace round trip | `test_end_to_end.py`, `test_portability.py`, `scripts/demo.sh` |
| T02 intermediate reanalysis | `test_replay.py` |
| T03 fork before a decision | `test_forks.py::test_t03_*` |
| T04 changed-request replay rejection | `test_decisions.py` |
| T05 no inherited future | `test_forks.py::test_t05_*` |
| T06 same spec, distinct invocations | `test_decisions.py` |
| T07 dependency invalidation | `test_forks.py::test_t07_*` |
| T08 corruption and missing closure | `test_portability.py` |
| T09 no host/vendor dependency | `test_portability.py`, `test_replay.py`, `test_end_to_end.py` |
| T10 crash-safe submission | `test_jobs.py` |
| T11 ledger and fork budgets | `test_budgets.py`, `test_forks.py::test_t11_*` |
| T12 safe import | `test_portability.py` |
| T13 secret and evaluator isolation | `test_portability.py`, `test_evaluation.py` |
| T14 checkpoint capability honesty | `test_controller.py` |
| T15 atomicity, ordering, versioning | `test_core.py` |
| T16 evaluation is a separate derivation | `test_evaluation.py` |
| SoL-Pi M1 fixtures (handles, compaction, reducer, fusion) | `test_context.py` |

T17–T25 need a real solver, a live Pi integration, or later milestones and
have not been run.

## Limitations

- Fixture costs are simulated and priced from a fixture table; they are not
  spend. Unknown costs are reported as unknown, never zero.
- One authoritative writer per run; no locking for concurrent writers.
- Forks require quiescent checkpoints. In-flight forks are not supported.
- The Pi adapter is a typed seam and a mock. No Pi, SoL-Pi, or Prime code is
  installed or called; every Pi extension point in
  [`docs/ADAPTER_SEAM.md`](docs/ADAPTER_SEAM.md) is marked unverified.
- Scientific tolerances, task membership, and the expert convergence check are
  unresolved; see [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md). Upstream
  revisions are pinned in [`provenance/sources.json`](provenance/sources.json).
- `RunView` is a snapshot taken at construction and must be rebuilt after new
  events.

## Next: M2

A local solver adapter, real output parsers, a few reviewed fixed-input
scientific cases with authentic outputs, and the external evaluator skeleton
(BUILD_SPEC §6). M2 should start only after this offline slice is reviewed.
