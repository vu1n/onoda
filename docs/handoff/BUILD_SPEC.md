# Build specification

## 1. Goal and ownership

Create a scientific task environment that can be operated by different agent harnesses while retaining portable evidence, reproducible intermediate steps, and auditable branches. The initial domain is multireference chemistry. The data model must not encode chemistry into the storage engine.

Own the task definitions, execution boundary, provenance, scientific observations, independent grading, and promotion criteria. Borrow general agent execution and optimization machinery. See [source notes](SOURCE_NOTES.md) for verified external capabilities and uncertainties.

The system should answer:

- What exactly did this run know, request, execute, and observe?
- Can another machine inspect and recompute a particular result?
- What happens when we branch here with a different procedure or decision model?
- Did an improvement generalize, and what did it cost including unsuccessful exploration?

Success is not merely producing a transcript, a converged solver output, or a favorable model-generated verdict.

## 2. Architecture

```text
Experiment manifest + versioned task inputs
                    |
       Trusted controller / budget ledger
                    |
    +---------------+------------------+
    |                                  |
Agent adapters                       Job adapters
Prime first; others later            Fixture -> local ORCA -> SLURM
    |                                  |
    +--- typed operation gateway ------+
                    |
        Immutable evidence + checkpoints
                    |
       Observation / diagnostic adapters
                    |
      Agent-visible view        Protected evaluator view
                    |                       |
          case-memory proposals       versioned verdicts
                    |                       |
        reviewed memory releases      comparison reports
                    |
       GEPA candidate evaluation on development tasks
```

The controller, budget enforcement, authoritative records, and protected evaluator run outside the agent's writable environment. An agent uses scoped gateway capabilities rather than direct credentials to the solver scheduler or unrestricted shared storage. Audit all subagent work through the same boundary.

### Suggested implementation stack

Use a Python package for the domain-neutral core and chemistry adapters. Select a supported Python version after checking dependency compatibility; pin it and dependencies. Expose a local CLI and typed service methods. Bridge to Prime's existing extension surface rather than reimplementing its host. Prime's host and Python-facing runtime are distinct integration surfaces; inspect them before designing hooks. [S3–S5]

Use immutable local files plus a rebuildable SQLite index initially. The canonical export must not depend on SQLite, a cloud bucket, an embedding service, or a vendor tracing account. Add remote object storage or an existing execution substrate later behind interfaces. Do not make Pillbox, LangGraph, or a hosted service a first-release dependency.

Optional Verifiers integration should wrap the environment after the core is stable. It must not become the exclusive representation of tasks, traces, or scores. [S9]

### Modules to implement

| Module | Contract |
|---|---|
| `core/artifacts` | Store and verify immutable bytes; resolve logical references without host paths. |
| `core/records` | Versioned task, step, invocation, checkpoint, event, and evaluation schemas. |
| `core/controller` | State transitions, safe checkpoints, dependency tracking, crash recovery. |
| `core/budgets` | Reservations, usage settlement, aggregate run/branch/experiment limits. |
| `core/portability` | Export, import, closure validation, capability inspection, schema migration. |
| `core/replay` | Pure inspection replay and explicitly selected deterministic reanalysis. |
| `core/forks` | Immutable parent links, configuration diffs, visible-state projection, invalidation. |
| `adapters/jobs` | Mock/local/scheduler submission, reconciliation, cancellation, collection. |
| `adapters/agents` | Prime and later harness integrations; normalized observable call records. |
| `chemistry` | Dataset import, solver input validation, output parsers, diagnostic evidence. |
| `evaluation` | Protected matching/adjudication, scores, tolerances, and metric aggregation. |
| `memory` | Case records, frozen releases, provenance-aware retrieval, candidate lessons. |
| `optimization` | GEPA candidate runs and promotion; no access to held-out answers. |

These are responsibility boundaries, not a requirement to create twelve services.

## 3. Task and tool interfaces

A task has a stable ID; a source snapshot; input artifact references; explicit units; permitted knowledge; target specification; budget profile; and split membership. The evaluator additionally owns reference answers and evaluation policy. Separate the two views physically and by authorization.

The job gateway exposes operations equivalent to `prepare`, `submit`, `status`, `cancel`, and `collect`. The observation layer exposes `parse` and `diagnose`. Each operation creates typed records and commits its inputs and outputs before they become visible to downstream decisions. These are our interfaces, not assumed upstream API names.

Submission accepts a reviewed executable configuration, input artifacts, and a durable idempotency key. The external controller—not the model—assigns job resources and enforces limits. Generated scripts run in isolation. A retry gets its own invocation record even when its specification matches an earlier attempt.

Record parser/version information, the exact source output range or artifact, scientific units, missing fields, and uncertainty. Preserve original numeric strings alongside normalized values where precision matters. Never replace absent diagnostics with zero or an invented default.

An orbital/state mapping must declare the exact wavefunction artifact and configuration that produced it. A consumer rejects mappings from a different artifact. Treat all other domain-specific compatibility checks as explicit, tested contracts.

Do not encode expert judgment inside an ostensibly neutral helper without documenting it. A parser extracts evidence; an active-space selector is a scientific policy and belongs in the ablation description.

## 4. Agent and decision integration

### Prime baseline

Use the existing Python-backed skill mechanism when practical. Provide functions that access the typed gateway and materialize the permitted current state. Keep raw logs and large arrays as artifacts, retrieving relevant slices as needed. Capture external calls, prompts that are actually observable, tool schemas, returned messages, generated code, context compaction, and child-agent relationships. [S3–S5]

A persistent REPL is a working convenience. Every supported resume/fork point must also have a serializable semantic checkpoint. Critical data cannot exist only in a Python variable, retained subagent, host cache, or daemon.

For controlled experiments, isolate each run's configuration/home directories and disable untracked refinement, inherited global memories, undeclared skills, uncontrolled communication, and background schedules. Audit that they are actually disabled; an instruction asking the agent to ignore them is insufficient. Use restricted network access and scoped storage views.

### Jev extension

Implement a `DecisionProvider` interface with mock, recorded, and live implementations. Its input includes a versioned question set, eligible actions, available evidence, and optional retrieved cases. Its result preserves the raw provider response and a normalized decision record. Do not invent SDK signatures; verify the pinned API when integrating. [S6–S7]

Code owns preconditions, numerical checks, budgets, and side effects. A decision provider may select among approved procedures or abstain. `insufficient_evidence`, `no_applicable_procedure`, and `escalate` must be representable. Do not equate a distribution-derived confidence field with verified scientific correctness.

Begin with shadow decisions that do not control execution. Once validated, allow reviewed workflow code to perform bounded routine steps without returning to the reasoning model each time. Each internal step still passes through the gateway and creates a checkpointable record. Independent judgments may share an observation; judgments requiring new evidence must wait.

A textual decision model is not automatically a visual expert. If an expert check requires images, preserve the images and leave the check unresolved until an appropriate review path is implemented. Never silently substitute an invented text heuristic.

### Shared memory

Separate per-run working state, append-only case evidence, and promoted procedure releases. Preserve successful, unsuccessful, and inconclusive cases with their original evidence. Model hypotheses are not equivalent to verified outcomes or independent expert judgments.

A frozen release defines exactly which cases and lessons may be retrieved. Record the query, filters, retrieval implementation, ordered candidate IDs, selected contents, and prompt rendering. A search index is rebuildable; it is not the only copy of experience. Prevent descendants, other test cases, and evaluation-only feedback from entering an agent's allowed view.

Candidate lessons may be proposed automatically. Publishing a shared rule requires explicit evaluation/review and creates a new version. Deduplicate evidence by source lineage so repeated summaries are not mistaken for corroboration.

### GEPA extension

Optimize only declared surfaces: advisory ladder text, diagnostic question wording, evidence selection, and approved routing parameters. Initial search should not edit arbitrary solver code or the evaluator. Each candidate, feedback packet, comparison, and rejection is a recorded artifact. [S8]

The optimizer sees development feedback only. Its experiment-level budget includes candidate generation, reflection, failed trials, and selection runs. A new policy is promoted by held-out development validation before the untouched test run. Do not claim that local reflection alone constitutes validated improvement.

### Later open-weight training

Preserve exportable, evidence-linked action examples and same-state comparisons now. Delay SFT/DPO until baseline behavior is measurable. A training export has its own immutable manifest: source records, split rules, transformations, labels, licenses, and model/configuration provenance. Exclude future observations and protected test data. Model agreement alone is not a scientific label.

## 5. Independent evaluation

Separate execution status, state identification, scientific checks, and reference error. Use `pass`, `fail`, and `unresolved` where appropriate; do not collapse missing evidence into success. Human adjudication records must include evidence, rubric/version, author identity or pseudonymous identifier, and timestamp.

A grader is a separate derivation over immutable run outputs. Updating it produces a new evaluation; it never overwrites a historic score. Both sides of a comparison use the same evaluator and target set. Record each scored transition's supporting assignment and references in the protected view.

State matching cannot rely solely on nearest energy. Uncertain scientific rules and reproduction tolerances require explicit decisions in the assumptions register. Reexecution tolerance and accuracy relative to a reference are different policies.

See [experiment plan](EXPERIMENT_PLAN.md). Do not implement a broad claim of chemical correctness from incomplete checks.

## 6. Milestones and release gates

| Milestone | Deliverable | Gate |
|---|---|---|
| **M0 — Establish contracts** | Source pins, assumptions, schemas, repository scaffold, no-spend defaults. | No fabricated task manifests, API methods, or scientific thresholds. |
| **M1 — Portable offline slice** | Synthetic execution, immutable records, checkpoints, export/import, replay, reanalysis, fork, costs. | Clean-workspace portability and adversarial tests pass. |
| **M2 — Scientific smoke tests** | Local solver adapter, parsers, fixed-input reviewed cases, external evaluator skeleton. | Real artifacts validate; discrepancies and unresolved checks are visible. |
| **M3 — Prime baseline** | Minimal skill/gateway integration; no-ladder versus advisory-ladder comparison. | Same resources/information; reproducible decision checkpoints; complete cost capture. |
| **M4 — Jev** | Mock/record/live decision adapter, shadow evaluation, then bounded active routing. | Tested abstention and failure handling; no unlogged fast-path decisions. |
| **M5 — Frozen shared memory** | Cases, snapshot export/import, filtered retrieval, lessons with provenance. | No future/test leakage; retrieval inputs and outputs reproducible. |
| **M6 — GEPA** | Reproducible candidate search with independent development evaluation. | All trials costed; policy promotion and evaluation are separate. |
| **M7 — Training** | Curated student datasets and model comparisons. | Dataset lineage, split integrity, and baseline cost-quality measurements exist. |

Build M0–M1 first. They must run without external credentials or licensed software. Scientific smoke tests should initially use a few tractable, documented fixed-input cases rather than a broad agent-driven batch. Do not fabricate these fixtures: acquire or generate authentic outputs with permission and preserve their provenance.

Unresolved author materials do not block an independently labeled reconstruction. Unresolved scientific evaluation blocks claims of reproduced scientific success, not development of the infrastructure.

## 7. Operational constraints

Import and replay are network-free by default. Live execution requires deliberate opt-in, scoped credentials, and explicit budget limits. Never automatically install or run code from an imported bundle. Preserve acquisition instructions and permitted redistribution metadata for all external dependencies; binaries need not be included for analysis replay.

Prime's process lifecycle is not a security sandbox. Run agent-generated code in an external restricted environment with no path or credential access to protected references or budget authority. [S3]

Add scheduling adapters after local execution works. Avoid duplicate remote jobs by persisting submission intent and reconciling uncertain outcomes before retries. Failure states must be recoverable without concealing whether a job may still be running.

## 8. Delivery reporting

For each milestone deliver: working code, pinned environment, tests, a minimal example, exact tested commands, a capability matrix, and an honest limitations list. Distinguish offline fixture tests from real scientific tests and live model tests.

A useful first demo is: **same exported observation, two forked decision policies, separately executed suffixes, an unchanged parent, and a comparison with complete provenance and costs.**
