# Assumptions register

Tracked project artifact required by `docs/handoff/SOURCE_NOTES.md` ("Initial
assumptions register") and `docs/handoff/SOL_PI_DELTA.md` §6. This is the
single place unresolved reconstruction decisions and M1 implementation
assumptions are tracked with a status and an owning milestone. Do not resolve
an item here by inventing a threshold, SDK method, or benchmark membership
(AGENTS.md rule 10); resolve it by inspecting the source and recording the
evidence, or leave it `unresolved`.

Status values: `unresolved` (not decided), `assumed` (a working default is in
force and must be revisited), `decided` (settled for the current milestone,
recorded here for traceability), `pinned` (see `provenance/sources.json` for
the resolved revision).

## From the handoff (SOURCE_NOTES.md)

| ID | Question | Status | Required behavior | Owner milestone |
|---|---|---|---|---|
| A1 | Exact historical task membership (which molecules/transitions the paper actually used) | unresolved | Build a transparent, independently derived manifest from QUESTDB (S2); never claim it matches the paper's exact selection. | M2 |
| A2 | Historical literature/context snapshot available to the original agents | unresolved | Version our own permitted corpus explicitly and record the difference from any historical snapshot. | M3 |
| A3 | Original prompts and evaluator | unresolved (paper's code/data section is a placeholder) | Reconstruct conservatively; label as reconstruction; keep evaluator answers outside agent-visible storage (BUILD_SPEC §5). | M2 |
| A4 | Operational meaning of "an attempt" (the paper's eight-attempt limit) | unresolved | Track jobs, configurations, retries, and actual resource use as separate portable-record fields rather than inferring the paper's accounting. | M1 |
| A5 | Semantics of the "new expert convergence check" mentioned by the user | unresolved | Leave a named, unresolved adjudication extension point (BUILD_SPEC §4, Jev extension); do not invent its criteria. A textual decision provider is not a visual expert. | M4 |
| A6 | Scientific state-matching / reproduction tolerances | unresolved | No nearest-energy-only matching and no arbitrary universal threshold; tolerances require explicit, evidenced decisions (BUILD_SPEC §5). | M2 |
| A7 | Solver (ORCA) availability and redistribution rights | unresolved for execution (no ORCA in this environment); outputs: see A25 | Support user-provided installation only; verify permission before distributing any solver artifact. `labrun.science.orca_local` runs only an operator-supplied binary and is disabled by default. | M2 |
| A8 | Exact Prime / Jev / GEPA / Verifiers revisions | partially resolved | See `provenance/sources.json`: Prime (S3), GEPA (S8), and Verifiers (S9) are pinned to a commit; Jev (S6, S7) remains `unresolved` — hosted docs site, no version identifier, no repository. | M0 |
| A9 | Provider-resolved model version (e.g. which underlying checkpoint served a request) | unresolved (may not be exposed by the provider) | Record whatever identifiers are observable in the response and state the resulting reproducibility limit; never infer an unexposed version. | M1 |
| A10 | Native Prime/Pi session portability | assumed: not portable | Implement portable semantic checkpoints independent of any running kernel; label native persistence/session features separately as harness-local conveniences. | M1 |
| A11 | API credentials, paid budget, and infrastructure authorization | assumed: none authorized | Offline defaults everywhere; live execution requires explicit operator configuration, credentials, and budget (never inferred from a present API key). | M0 / M1 |
| A12 | Original authors' private communications | assumed: out of scope | Do not export private messages or infer scientific instructions that were not provided in this packet. | M0 |

## From SoL-Pi (SOL_PI_DELTA.md §6)

| ID | Question | Status | Required behavior | Owner milestone |
|---|---|---|---|---|
| A13 | Pi / SoL-Pi revisions and extension API stability | pinned (revisions); API stability unresolved | `provenance/sources.json` pins SoL-Pi to commit `1559b5c...` (S13) and the Pi coding agent to npm `0.85.1` with its published integrity hash (S14). Whether SoL-Pi's extension points are stable across Pi 0.85.1 → 0.87.1 (current npm `latest`) is not verified; do not install past 0.85.1 without re-checking. | M0 |
| A14 | Whether ObservationPack thresholds (10 KiB archive, 2-request resend window) suit ORCA log sizes | unresolved (tuned on coding benchmarks) | Keep thresholds as configurable, recorded parameters; do not retune on chemistry test data. | M3b |
| A15 | Reducer completeness on scientific logs (does a compact receipt drop a convergence warning or root flip?) | unresolved | The deterministic `chemistry` parser stays authoritative; the evaluator never reads reducer receipts; measure diagnostics lost in receipts on dev cases before trusting them. | M3b |
| A16 | Mechanism transfer across models (paper reports lower activation on Opus 5 than the tuning model) | unresolved | Log activation rate per mechanism per model; make no transfer claim without our own data. | M3b |
| A17 | Prime's base Pi version and its compatibility with the SoL-Pi extension API | unresolved | Verify before attempting Route B (PrimeAgent-SoL per SOL_PI_DELTA §0); do not assume compatibility. | M3 (if Route B pursued) |
| A18 | Capability-gate tolerances for the two-gate promotion rule | not chosen | Predeclare and justify tolerances in this register before any capability/efficiency comparison; zero-increase tolerance for false-success claims is the one threshold already fixed (SOL_PI_DELTA §5). | before M3 comparisons; enforced at M6 promotion |
| A19 | Auto-research loop code (the search procedure that produced SoL-Pi's mechanisms) | unresolved (not released in the repository) | Reconstruct only from the published paper; label any reconstruction as such; do not claim it reproduces the authors' implementation. | M6 |

## M1 implementation assumptions (this build)

| ID | Question | Status | Required behavior | Owner milestone |
|---|---|---|---|---|
| A20 | How are compute/solver costs represented before a real solver is integrated? | assumed | Fixture costs are simulated numbers, never presented as real ORCA/provider spend; every simulated value is labeled as a fixture (AGENTS.md rule 11). | M1 |
| A21 | Can canonical records carry floating-point numbers? | decided | No. Floats are banned from canonical JSON (`labrun.canonical.canonical_bytes` raises `CanonicalizationError`); exact decimals are carried as strings. See `tests/test_core.py::test_floats_and_unsafe_ints_rejected`. | M1 |
| A22 | Can more than one process append to a run's journal? | decided | One authoritative writer per run. The store's commit order (object → record → event) and hash-chained journal assume a single writer; concurrent writers are out of scope until an explicit locking design is added. | M1 |
| A23 | Can a fork be taken from any point in a running trajectory? | decided | No. Forks require a quiescent, declared checkpoint boundary (`initialized`, `job-collected`, `diagnostics-committed`, `decision-committed-before-submission`, `finalized` — see `labrun.records.BOUNDARIES`); a fork mid-step is not supported. | M1 |
| A24 | Does resuming a run replay the original process/kernel state? | decided | No native resume. Only semantic restart from a serialized checkpoint is supported; a persistent REPL, subagent, or daemon is never the canonical record (AGENTS.md rule 6). | M1 |


## M2 implementation assumptions (this build)

| ID | Question | Status | Required behavior | Owner milestone |
|---|---|---|---|---|
| A25 | Where do authentic ORCA outputs come from without an ORCA licence here? | assumed | Vendored from cclib's public regression data (`cases/orca/cclib/`, BSD-3-Clause, commit pinned in each `*.case.yaml`, bytes pinned by sha256). Redistribution permission is taken from cclib's public BSD-3 distribution; the ORCA EULA's position on redistributing outputs has not been checked. These runs are recorded as acquired (`executed_here: false`), never as executions. Replace or confirm before any public release. | M2 |
| A26 | Who reviews the fixed-input cases? | unresolved | Every case is `pending-human-review`; each evaluation carries `case.review` so an unreviewed case cannot pass as reviewed. The declared request (roots per multiplicity) was transcribed from the inputs by the harness author. | M2 |
| A27 | Units of `FINAL SINGLE POINT ENERGY` | assumed | ORCA prints no unit token on that line; the parser records `Eh` by ORCA's documented convention. State lines carry printed unit tokens (`au`, `eV`, `cm**-1`), which are validated, never converted. | M2 |
| A28 | Multiplicity of EOM-CCSD / ADC(2) roots | decided | Not printed on `IROOT` lines, so it stays unknown (`None`) and state identification is `unresolved` for those cases. It is not inferred from `DoTriplet false` in the input. | M2 |
| A29 | Which cclib validates the parser? | assumed | `cclib==1.9rc1`, dev-only, used by `tests/test_orca_parser.py` as an independent cross-check. The latest stable release (1.8.1) cannot read the ORCA 6 files. Not a runtime dependency. | M2 |
| A30 | Reexecution tolerance between ORCA versions | unresolved (instance of A6) | `orca-reexecution@1` has no tolerance, so the ORCA 6.0 vs 5.0 comparison reports exact differences (e.g. 0.000006 au on the first singlet) and stays `unresolved`. A tolerance needs an assumptions-register row that justifies it. | M2 |

## Maintaining this register

When an item above is resolved, update its `Status` (and, for source pins,
add or update the matching entry in `provenance/sources.json`) instead of
deleting the row; keep the row so the history of the decision is visible.
Add new rows here rather than in code comments when a future milestone
surfaces a new unresolved question.
