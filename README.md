# onoda

Portable, forkable run records for scientific-agent experiments. An experiment
belongs to its data and provenance, not to the agent framework that ran it:
a run can be exported, imported on another machine, inspected offline,
partially recomputed from raw bytes, and branched from an explicit checkpoint
without losing lineage or cost accounting.

This is milestones **M0–M2** of the handoff in [`docs/handoff/`](docs/handoff/)
(v0.2; [`SOL_PI_DELTA.md`](docs/handoff/SOL_PI_DELTA.md) overrides conflicting
text, so Pi is the first agent adapter). M0–M1 run on a **completely synthetic
fixture**. M2 adds scientific smoke tests over **authentic ORCA outputs** that
were produced elsewhere (cclib's public regression data) and are recorded as
acquired, not executed here. No ORCA has been run by this harness, and no
chemistry experiment has been reproduced.

The handoff names the CLI `labrun`; in this repository the package and CLI are
`onoda`.

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

`onoda` implements the interface proposed in PORTABLE_RUN_SPEC §14. Every
command prints JSON.

```sh
onoda fixture run --run-id demo --store ./store-a
onoda checkpoint list demo --store ./store-a
onoda export demo --store ./store-a --profile agent-visible --out demo.tar.gz
onoda import demo.tar.gz --store ./store-b
onoda inspect demo --store ./store-b --capabilities
onoda replay demo --store ./store-b --offline
onoda reanalyze demo --step parse --store ./store-b [--transform fixture-parser@2]
onoda fork demo --checkpoint <digest> --declaration fork.yaml --new-run demo-branch --store ./store-b
onoda resume demo-branch --mode fixture --store ./store-b
onoda compare demo demo-branch --store ./store-b --include-cost-lineage
onoda evaluate demo-branch --store ./store-b --evaluator-store ./evaluator
```

The mock scheduler stands in for an external system, so its state lives beside
the store (`<store>.scheduler/`) and is never exported.

M2 scientific smoke cases:

```sh
onoda case list
onoda case ingest orca6-dvb-tddft --run-id td6 --store ./store-a
onoda case evaluate td6 --store ./store-a --evaluator-store ./evaluator --reference-case orca5-dvb-tddft
# Live execution needs an operator-supplied ORCA and every setting stated:
onoda case ingest orca6-water-mp2 --run-id live --store ./store-a --execute \
  --enable-live-execution --orca-binary /opt/orca/orca --work-root ./orca-jobs --max-wall-seconds 600
```

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
| `science.contract` | M2 observation schema (exact decimal text, units, line evidence, explicit problems) and sha256-pinned case manifests |
| `science.orca_parser` | Deterministic ORCA 5/6 output parser (`orca-parser@1`) |
| `science.orca_local` | Local ORCA scheduler for an operator-supplied binary; disabled by default, idempotent by key |
| `science.evaluator` | External evaluator skeleton: execution, parse integrity, state identification, reference comparison under a declared policy |
| `science.runner` | Records a case as a run (acquired or executed), parses it, and evaluates it in a separate store |

## Capability matrix

What was actually demonstrated, and on what. "Fixture" means the synthetic
workflow in `onoda.fixture`; none of these rows is a scientific result.

| Capability (PORTABLE_RUN_SPEC §1) | Status | Evidence |
|---|---|---|
| Inspect / replay | Demonstrated on fixture, offline, after import into a fresh store | T01, T09; `tests/test_replay.py`, `tests/test_end_to_end.py` |
| Reanalyze | Demonstrated for the pure parse step; new derivation receipts, originals untouched | T02; `tests/test_replay.py` |
| Reexecute | Fixture programs via the mock scheduler. Local ORCA execution is implemented but has only run a labelled stand-in executable; no ORCA was available | `inspect --capabilities`, `tests/test_orca_local.py` |
| Parse authentic solver output | ORCA 5.0/6.0 TD-DFT, EOM-CCSD, ADC(2), STEOM-CCSD, MP2 outputs; values cross-checked against cclib | `tests/test_orca_parser.py` |
| Evaluate against a reference | ORCA 6.0 vs 5.0 TD-DFT: exact differences reported, verdict `unresolved` (no justified tolerance, A6) | `tests/test_smoke_cases.py`, `scripts/demo.sh` |
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
| T17 authentic fixed-input case (partial: acquired outputs, not executed here) | `test_orca_parser.py`, `test_smoke_cases.py` |
| T18 honest partial outputs | `test_orca_parser.py` (truncation, missing diagnostics, malformed numbers, wrong units, conflicting listings), `test_smoke_cases.py` |

T17 is only partly met: its "execute a reviewed fixed input with authorized
software" step needs an operator-enabled ORCA and a human case review
(assumptions A7, A26). T19–T25 need a live Pi integration or later milestones
and have not been run.

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
- The M2 cases are cclib's small STO-3G regression inputs, not the paper's
  task set (A1), and none has been reviewed by a human yet (A26). Their
  redistribution rests on cclib's BSD-3 licence (A25).
- EOM-CCSD and ADC(2) outputs print no multiplicity on their root lines, so
  state identification is `unresolved` for those cases (A28).
- Case runs support inspect, export/import, and offline replay. Fixture-only
  commands (`resume`, `fork`, `reanalyze`, `compare`, `evaluate`) refuse them,
  and `inspect --capabilities` reports those as unsupported by the record. An
  interrupted case run is continued by rerunning `onoda case ingest` with the
  same run id.
- `case evaluate` re-checks an acquired run against the pinned case (manifest
  digest, log sha256, a fresh parse). A live run's log has no pinned digest,
  so its execution receipt and stand-in flag are trusted as recorded; do not
  grade live runs from imported bundles you did not produce.
- A live run's timeout kills the ORCA process group, but ORCA itself has only
  been simulated by a stand-in executable here.

## Next: M3

Pi baseline and Pi + RLM extension on a frozen development set (SOL_PI_DELTA
§6). Before comparisons: a human review of the M2 cases, an ORCA run of at
least one pinned input on licensed hardware, and justified tolerances (A6, A18).
