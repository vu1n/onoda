# Portable run contract

**Priority:** P0. **Format target:** versioned, domain-neutral, local-first.  
This is a proposed implementation contract, not an existing Prime or Jev format.

## 1. Capabilities must have different names

| Capability | What it does | What it does not establish |
|---|---|---|
| **Inspect / replay** | Reconstructs history and visible state from recorded events and outputs; no external calls. | That a solver or model would return those outputs again. |
| **Reanalyze** | Reruns a selected parser, deterministic transform, or evaluator against saved immutable inputs. | That the underlying calculation was reproduced. |
| **Reexecute** | Runs a selected calculation or model call again using recorded inputs and a declared environment. | Bitwise equality or identical live model behavior. |
| **Resume** | Continues a paused/crashed run at a supported boundary with the same declared policy/configuration. | Arbitrary restoration of a Python stack or a different provider's hidden state. |
| **Fork** | Creates a new run from a checkpoint, with explicit changes and immutable ancestry. | That original downstream outputs remain valid after a change. |
| **Repeat experiment** | Executes fresh trials of a pinned experiment and compares their distributions/outcomes. | That repeated trials must have identical trajectories. |

Every report declares the operation performed, the inputs and environment available, the comparison policy, and the demonstrated result. Avoid a single unqualified `reproducible: true` flag.

A replay can be exact with respect to recorded history while a new live run differs. Record unknown model versions or hidden provider state as limitations instead of promising deterministic behavior from a seed.

## 2. Canonical objects, not opaque sessions

### Required record types

| Type | Minimum contents |
|---|---|
| **Artifact descriptor** | SHA-256 of raw bytes, byte length, media type, encoding/units where relevant, visibility, source/license metadata, availability. |
| **Task manifest** | Stable task ID, input/source digests, permitted context, split/group IDs, target specification, profile versions. |
| **Step specification** | Operation type, immutable named input refs, relevant configuration/code/environment refs, schema version, determinism declaration. |
| **Invocation receipt** | Unique invocation ID, specification digest, actual inputs/outputs, parent invocations, observed status, attempts, model/job metadata, costs, timestamps. |
| **Event** | Unique ID, run ID, sequence, actor, causal links, type, payload refs, previous-event digest, timestamp. |
| **Checkpoint** | Consistent committed frontier, visible state refs, next workflow phase, current policy/memory/context, budget ledger refs, external-job state, supported restoration capabilities. |
| **Fork declaration** | Parent run and checkpoint digests, intervention diff, adapter/context translation, invalidation boundary, visibility cut, budget treatment, reason. |
| **Evaluation** | Output refs, protected reference/rubric versions, assignments, unresolved items, metrics, costs, evaluator provenance. |
| **Memory release** | Immutable eligible case/lesson IDs, training lineage, curation policy, retrieval configuration and version. |

Use JSON/JSONL for structured records and original formats for raw artifacts. Keep schema versions explicit. Large arrays may use a documented open format; include dimensionality, dtype, ordering, units, and producer version. Never require an arbitrary object deserializer to inspect a run.

A SQLite index, hosted trace, vector index, native Prime session, or VM snapshot can accelerate operations, but must be disposable without losing the scientific record.

### Two different identities

**A specification digest is not an execution ID.** Repeating an identical model request or solver configuration produces a new invocation receipt. Identical specifications may have different outputs. Never overwrite or merge those receipts because their inputs match.

An artifact's identity is its byte hash. Store bytes unchanged. A human-friendly molecule, run, or step ID is metadata, not a substitute for integrity validation.

For canonical structured manifests, use an existing RFC 8785-compatible implementation and interoperability tests rather than `sort_keys=True` as an undocumented cross-language standard. [S10] Reject unsupported numeric representations. Preserve exact scientific decimal values as strings with explicit units when necessary. Define precisely which envelope fields are hashed; do not include a record's own digest in its hash input.

Content hashes provide integrity checks, not proof of author identity or scientific validity. Optional signed release roots can be added later; do not confuse an unsigned self-consistent bundle with trusted content.

## 3. Dependency graph and event journal

The run is a directed acyclic graph of actual invocations and artifacts. A retry or feedback loop creates new nodes in time, not a cycle pointing from an artifact into its own history. Inputs include documents, retrieved memory, policies, code, prompts, environment, and tool results—not just calculation files.

Use immutable artifacts and receipts. An append-only per-run journal records lifecycle events. A trusted coordinator assigns sequence numbers; timestamps alone do not define causal order. Start with one authoritative writer per run, not a distributed consensus system. Preserve parent/child relationships for subagents and concurrent jobs.

Commit order should make it impossible to publish a checkpoint that references an uncommitted output. For local storage: write temporary bytes, verify/hash, atomically commit objects, then publish receipts and journal events. Recover or quarantine partial writes after a crash. Derived indexes are rebuilt from committed canonical records.

The graph must include all declared dependencies required to reproduce a step. Undeclared reads are a reproducibility defect. Where the environment cannot observe a dependency, mark the step incomplete for reexecution and retain its inspection capability.

## 4. Proposed bundle layout

```text
bundle/
  manifest.json                 # format/version, roots, inventory, capabilities
  schemas/                      # definitions needed to interpret records
  records/
    runs/<id>.json
    steps/<digest>.json
    invocations/<id>.json
    checkpoints/<digest>.json
    evaluations/<id>.json       # only in authorized exports
  events/<run-id>.jsonl
  objects/sha256/<prefix>/<rest>
  environment/
    lockfiles/
    build-recipe.json
    requirements.json
  provenance/
    sources.json
    licenses.json
    omissions.json
  README.md                     # human-readable capability/limitation summary
```

The exact layout may evolve before implementation, but references must be relative/logical, never dependent on the originating user's home directory, bucket, database primary key, presigned URL, or framework session ID. Record remote source locations as provenance, not as the only way to identify bytes.

An export manifest inventories every included file with size and digest, excluding its own envelope as specified by the format. Its transitive dependency closure must be machine-checkable. Export a directory or standard archive. Fix archive ordering and metadata when byte-identical archive production is supported; semantic bundle identity should not depend on compression timestamps.

Support exporting one run, a selected subgraph, or a branch with its required ancestor closure. Independent optional RO-Crate export can be considered after the native contract works; the standard provides structured research-object metadata, not our execution engine. [S11]

## 5. Checkpoints: explicit supported boundaries

Initial checkpoint boundaries:

1. Task initialization after permitted inputs and configuration are committed.
2. Calculation completion after the full required output set is committed.
3. Parsing/diagnosis completion before a next-action decision.
4. Decision completion before submitting its side effect.
5. Result collection and task-finalization boundaries.

The critical fork is **after an observation, before the next decision**. To compare decision policies, branch there. Branching after a recorded decision does not test a different decision unless that node is invalidated and rerun.

A checkpoint stores materializable state: current task/configuration, observations, accumulated allowed history, selected memory release, actual context messages or their serialized representation, compaction summary, tool schemas, workflow phase, random state where available, job identities, and budget reservations. It also identifies the last committed frontier and visible ancestry cut.

Do not depend on pickling a persistent Python kernel. Rebuild required variables through tested bootstrap code operating on immutable artifacts. Optional native/VM snapshots can support a narrower same-environment resume; their format and portability limits must be declared separately.

If a scientific operation is hidden inside an arbitrary long Python cell, pause at gateway boundaries. Do not promise restoration in the middle of that cell's stack. A checkpoint may explicitly support a **semantic restart** with task state but not an exact native session continuation.

For initial forks, require a quiescent checkpoint: no uncommitted tool responses, active child agents, or unresolved submissions. For resume, reconcile pending jobs by durable ID before progressing. Later support for in-flight forks must define ownership and cancellation semantics before use.

Across harnesses, translate semantic task state via an explicit versioned adapter and log the new rendered context. Label this a **cross-harness semantic fork**, not identical conversational continuation. The parent remains readable without either harness installed.

## 6. Capturing a model decision

Preserve the observable request body and response, ordered context, tool definitions, decision alternatives, policy version, retrieved contents, provider/model identifiers, exposed resolved version/fingerprint, SDK version, sampling settings, usage, request IDs, timestamps, and errors. Store secrets separately; never put bearer tokens or signed URLs in exported call bodies.

If hidden provider instructions or an exact backend version are unavailable, record that fact. Do not manufacture them. Capture user-visible output and tool actions; private internal reasoning is not required for this contract.

Offline call replay requires an exact supported request/dependency match. When the request changes, fail closed with `replay_mismatch`; do not serve the nearest cached answer. A live call is a new invocation even if every observable setting matches.

A new model decision can be compared at the same saved observation. Its scientific consequences require executing the newly chosen action and ensuing suffix. Recorded outputs from the old action are not evidence for a counterfactual action.

## 7. Fork semantics and invalidation

A fork creates a new run ID, preserves the immutable parent/checkpoint relation, and stores a machine-readable intervention diff. Typical changes: decision provider, ladder version, permitted memory snapshot, sampling configuration, or execution policy. A change to task geometry or scientific method creates a new task/configuration lineage, not an unnoticed replacement under the old identity.

Reuse only committed inputs and artifacts in the checkpoint's allowed ancestor closure. Invalidate every changed node and dependent descendant. Artifacts remain in the store for historical inspection, but are excluded from the child run's active state and caches.

Specific requirements:

- A changed decision cannot inherit the old decision's tool result.
- A changed geometry, basis, state averaging, or wavefunction invalidates dependent numerical data and mappings unless an explicit tested compatibility rule permits reuse.
- A changed parser creates a new observation derivation. Downstream decisions are rerun or explicitly marked as old-policy history, never silently treated as current.
- A changed evaluator creates an evaluation branch over frozen outputs. It does not rewrite the executed scientific trajectory.
- Changing memory after a checkpoint is an explicit intervention. A branch cannot acquire outcomes from its own parent run's later events by retrieving an unrestricted current memory database.

For controlled comparisons, inherit the identical permitted evidence and retrieval scope up to the fork unless memory is the treatment. The parent run may have finished before the fork is created; only its checkpoint-visible past is eligible.

Never automatically merge two scientific execution histories. A reviewed lesson may draw on both branches with cited provenance; the branches themselves stay distinct.

## 8. Replay, reanalysis, and cache policy

Inspection replay reconstructs the recorded event/state sequence without initiating network calls, jobs, or model invocations. It must work when all provider credentials are absent and the original database/session has been deleted.

Reanalysis executes only explicitly requested, trusted transforms against captured inputs. For example, rerun a parser on saved output, then run a specified grader. Record this as a new derivation and compare its result with the original. A bug fix is not a reason to alter the old output or claim the old parser produced the new answer.

Scientific reexecution requires available solver/runtime dependencies and a predeclared comparison policy. Report bitwise equality separately from numerical agreement, matched state identity, and aggregate metric agreement. Record observed differences, not just pass/fail.

Memoization must be opt-in and operation-specific. Exact pure transforms may reuse results after matching inputs, code, configuration, and environment constraints. Model calls and live scientific trials are not silently memoized in fresh-run benchmarks. Intentional reuse is a new reuse record referencing the original receipt, not a new successful execution.

A cache hit must never bypass scientific state-compatibility checks or access controls.

## 9. Budgets and branch accounting

Record resource units even when monetary prices are unavailable: input/output/cached/reasoning tokens where exposed, model calls, solver submissions, retries, allocated resources, measured CPU time when available, elapsed time, memory, queue time, and cancellations. Distinguish measured from estimated consumption. Record price-table version for monetary conversions; unknown price is not zero.

Report three views:

1. **Marginal branch cost:** actual new work performed after the fork.
2. **Inherited prefix cost:** the historical work that produced the starting state, counted once per unique ancestral invocation.
3. **Full-path attributed cost:** the inherited prefix plus the branch's new work, with reuse explicitly labeled.

Also report actual physical experiment spending across all unique executed invocations. Do not add full-path branch costs to estimate the bill: that double-counts shared prefixes. Optional estimated replacement cost belongs in a separate field and must not masquerade as observed spending.

By default, a matched-budget fork inherits the original consumption and reservations and receives only the remaining allowance. A fresh or larger allowance is an explicit treatment. Persist a quota identity above branches so fork creation cannot evade an experiment-level cap.

Reserve resources before side effects and reconcile actual usage afterward. Use limits for individual calls/jobs plus aggregate admission control. In-flight work can create unavoidable exposure; record reservation assumptions and residual overrun risk rather than promise perfect instantaneous billing control.

Shared-prefix trials test conditional decisions, not independent full-run performance. Replayed or cached prefixes are not fresh successes.

## 10. Side-effect recovery

Before submission, commit an intent with a stable idempotency key. Persist provider/scheduler job identity as soon as known. On a crash between submission and acknowledgment, reconcile the external system before retrying. An unknown outcome remains `submission_uncertain` rather than triggering blind resubmission.

Resume must not duplicate paid jobs, calls, or external writes by replaying an old cell. A fork may reuse a completed permitted artifact, but cannot inherit control of an in-flight parent job in the first release.

## 11. Export visibility and safety

Support profiles that distinguish agent-visible data, authorized researcher/evaluator data, and shareable redacted exports. Do not create an agent bundle that secretly includes reference answers in a different folder or a globally addressable object store. Access checks apply to object references as well as file paths.

A full bundle is closed over dependencies for its **declared capability**, not necessarily every possible operation. An analysis-complete bundle may omit solver binaries while supporting offline parsing/grading. A thin bundle lists missing artifacts with digests, reasons, acquisition requirements, and the capabilities affected. Importing it must report the limitation rather than fabricate data or fetch automatically.

Redaction produces a new derivative bundle with explicit omissions and reduced capabilities. Never keep the original exact-replay claim after changing a captured prompt or removing necessary evidence. Maintain original records only in the authorized store.

Capture license/redistribution metadata. Default external executables and restricted documents to operator-supplied requirements until permission is verified. A container recipe is useful, but does not by itself grant rights to redistribute binaries or guarantee future availability.

Import must validate checksums, schema versions, dependency closure, allowed paths, file counts, byte limits, and compression expansion limits. Reject path traversal, links escaping the bundle, executable import hooks, unsupported object deserialization, and unexpected remote fetches. Treat imported code as inert data until separately reviewed and explicitly executed in isolation.

## 12. Environment and longevity

Record code revision plus dirty-tree patch or content snapshot, dependency locks, build recipe, executable digests, relevant libraries, operating system, hardware/architecture, thread/process configuration, random seeds, locale, and environment variables relevant to outputs. Credentials and irrelevant host identifiers must be excluded.

A container image digest is not sufficient if the image can no longer be obtained. Record a reconstruction recipe and dependency availability. Include distributable code/dependencies or offline installation artifacts when an offline-execution capability is claimed. Otherwise distinguish clean-machine reconstruction with acquisition steps from fully offline reexecution.

Support versioned readers and explicit migrations that create new records, retaining source digests. Do not mutate historic bundles in place. Unknown versions fail with a useful compatibility report.

## 13. Acceptance summary

The contract is satisfied when a fresh environment can inspect the run without vendor state, reexecute selected deterministic analyses from raw bytes, explain missing scientific/live dependencies, and produce a correctly invalidated, independently attributable branch from a supported checkpoint.

The full tests are in [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md). Intermediate reproducibility is a demonstrated capability of particular steps—not a marketing label for the whole system.

## 14. Proposed operator workflow

These commands are interface examples for Codex to implement, not existing commands. Exact syntax may be adjusted consistently, with tests and documentation updated together.

```sh
# Generate an entirely synthetic run; no model or scientific solver.
labrun fixture run --run-id demo --store ./store-a
labrun checkpoint list demo --store ./store-a

# Transfer without depending on the original store or agent session.
labrun export demo --store ./store-a --profile agent-visible --out demo.tar.gz
labrun import demo.tar.gz --store ./store-b
labrun inspect demo --store ./store-b --capabilities
labrun replay demo --store ./store-b --offline

# Recompute an explicit pure transform from captured inputs.
labrun reanalyze demo --step parse-fixture --store ./store-b

# Branch from the listed pre-decision checkpoint; fill the actual digest.
labrun fork demo --checkpoint <checkpoint-digest> \
  --declaration fork.yaml --new-run demo-branch --store ./store-b
labrun resume demo-branch --mode fixture --store ./store-b
labrun compare demo demo-branch --store ./store-b --include-cost-lineage
```

`inspect --capabilities` must distinguish supported, blocked-by-missing-input, blocked-by-environment, and unavailable-provider-state cases. `resume` must not silently turn a semantic restart into an exact-native-resume claim.
