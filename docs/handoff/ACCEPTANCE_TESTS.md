# Acceptance tests

Tests are requirements for the implementation. This handoff does not claim they have been executed against a harness.

## M0–M1: portable offline core

### T01 — Clean-workspace round trip

Given a deterministic synthetic run, export it and remove access to the original store, SQLite index, provider credentials, and native agent session. Import it into a fresh directory with network disabled. Reconstruct its event history, materialize its declared checkpoint, and reproduce the same normalized final fixture state. Assert zero external calls.

### T02 — Intermediate reanalysis, not just log playback

Starting from imported raw fixture bytes, execute the pinned parser and deterministic transform again. Compare normalized output digests. Create new derivation receipts, preserve the original receipts, and label this as fixture reanalysis rather than scientific replication.

### T03 — Fork before a decision

Create two branches from the same committed observation checkpoint. Give them different deterministic mock decision policies. Assert identical inherited evidence, distinct decision receipts, independent suffix execution, correct parent/checkpoint links, and byte-identical unchanged parent artifacts.

### T04 — Changed-request replay rejection

Change any decision-relevant field: question text, evidence digest, tool schema, policy version, or candidate ordering. A replay lookup must reject the old response with an explicit mismatch. No fuzzy cache hit or unannounced live fallback is allowed.

### T05 — No inherited future

Complete a parent run with a successful later outcome and a memory lesson. Fork from an earlier checkpoint. Assert the later outcome, descendant artifacts, and derived lesson are inaccessible through files, search, memory, or content-hash lookup. A provenance filter must reject disguised summaries of the same future evidence.

### T06 — Same specification, distinct invocations

Execute a mock nondeterministic operation twice with identical specifications and different outputs. Assert two receipts coexist, no result is overwritten, and replay identifies the recorded invocation rather than guessing which output was meant.

### T07 — Dependency invalidation

Change a synthetic task input, parser version, policy, or wavefunction-like dependency. Assert affected descendants cannot be reused in the branch. Valid ancestors remain reusable. A mapping tied to one wavefunction digest is rejected when applied to another.

### T08 — Corruption and missing closure

Alter one byte in an exported object; verify import fails with a useful integrity error. Remove a required object; verify the importer reports its digest and affected capabilities. A declared thin bundle may remain inspectable but cannot claim a missing capability or auto-fetch the object.

### T09 — No host/vendor dependency

Export from one absolute directory and import under another. Delete all local indexes and native session metadata. Replay and pure reanalysis still work using logical references. Inspecting the graph must not import Prime, call Jev, or initialize a vector service.

### T10 — Crash-safe submission

Simulate a crash after submission intent, after remote acceptance but before acknowledgment, and after output commit but before indexing. Resume must reconcile known or uncertain job state and never duplicate a side effect. Uncertain external state stays explicit until resolved.

### T11 — Ledger and fork budgets

Use fixture costs with a shared prefix and two suffixes. Assert each full-path cost includes the prefix once, the marginal costs exclude it, and actual total spending counts shared physical work only once. A fork inherits spent/reserved allowance by default and cannot bypass an experiment-level cap. Unknown costs are not zero.

### T12 — Safe import

Reject path traversal, absolute-path archive entries, escaping symlinks/hardlinks, unsupported schemas, oversized expansions, and import-time execution hooks. No pickle loading, shell execution, provider contact, or dependency installation occurs during import.

### T13 — Secret and evaluator isolation

Seed test-only credentials and protected reference values. Assert neither appears in the agent-visible export, logs, error payloads, or object lookup results. Authorized evaluator exports may include references but never credentials. Redaction creates a derivative capability report instead of pretending byte-identical replay is preserved.

### T14 — Checkpoint capability honesty

A checkpoint missing restorable native state must refuse exact session resume while allowing a documented semantic restart where supported. Imported semantic state must not claim to restore arbitrary Python variables, running child processes, or provider-hidden state. Cross-harness translation creates a recorded context artifact.

### T15 — Atomicity, ordering, and versioning

Interrupt writes at each commit boundary. A checkpoint must never reference an unpublished output. Rebuild indexes, preserve causal event ordering, reject unknown schema versions, and test one explicit migration that leaves the original artifact unchanged. Verify canonical hashing against nontrivial Unicode/numeric test vectors for the selected library.

### T16 — Evaluation is a separate derivation

Run two fixture graders over identical immutable outputs. Assert distinct evaluator versions and scores coexist, historic scores are unchanged, and comparison refuses to silently mix evaluator versions.

## M2–M3: scientific environment and Prime

### T17 — Authentic fixed-input scientific case

With authorized software and resources, execute a reviewed fixed input, preserve all required outputs, parse real values with units and source evidence, and apply a declared comparison policy. Report numerical differences and unresolved checks. A mock cannot satisfy this test.

### T18 — Honest partial outputs

Exercise truncated logs, missing diagnostics, malformed numbers, wrong units, and ambiguous state assignments. Each produces explicit missing/error/unresolved states. No default numeric success or nearest-energy-only assignment is accepted.

### T19 — Controlled Prime baseline

Run with isolated harness state, pinned permitted context, and external budget authority. Verify all model/subagent/tool work is captured and global skills/memories/background processes do not leak across tasks. Confirm direct evaluator storage access and unauthorized network access fail at the environment boundary.

### T20 — Portable Prime checkpoint

Capture a real gateway-boundary checkpoint. Import its permitted semantic state into a clean worker and render the same recorded decision evidence. If exact native resume is unsupported, demonstrate semantic restart and record its new context. No double-submission occurs.

## M4–M6: decision, memory, and optimization extensions

### T21 — Jev shadow and abstention

First use a mock/recorded provider, then a separately authorized live integration. Preserve raw/normalized responses, exposed model identity, and all costs. Test insufficient evidence, unavailable service, malformed responses, low validated confidence, and incompatible input modalities. None may silently trigger an unapproved action.

### T22 — Fast path remains observable

Run multiple routine steps inside one workflow invocation without a reasoning-model call between every step. Verify each decision, job, result, and checkpoint is separately recorded and individually forkable at the declared barriers.

### T23 — Memory portability and provenance

Export a frozen release, rebuild its index, and reproduce the same selected case packet using captured ranked results. A live retrieval rerun is a new derivation if ordering changes. Verify eligibility filtering, deduplicated source lineage, and rejection of test-derived or future-derived lessons.

### T24 — GEPA cannot alter the grader

Attempt changes to evaluator code, protected targets, budget authority, and test-set access. Reject them. Candidate policies, failed evaluations, reflection calls, and selection costs remain in the experiment record. A promoted policy has an immutable diff and supporting development evaluations.

### T25 — Fresh trial versus reused prefix

Run a branch-local comparison and a fresh end-to-end comparison. Reports must identify which is which and prevent counting replayed prefixes or multiple descendants as independent complete successes. Changed action outputs must be obtained from the changed action, not borrowed from a recorded future.

## Delivery checklist

M0–M1 requires T01–T16 using deterministic offline fixtures and a documented clean-directory demo. Later tests cannot be reported as passing without their real prerequisites. CI should separate `unit`, `offline-portability`, `solver-integration`, and `live-provider` suites, with the latter two disabled by default and clearly reported as not run when unavailable.

A skipped test is not a pass. A capability that exists only behind mocks must be labeled accordingly.
