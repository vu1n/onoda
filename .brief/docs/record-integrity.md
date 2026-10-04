---
id: record-integrity
project: onoda
type: decision
status: active
signoff: required
title: "Run records are immutable, single-writer, float-free, and restart only from semantic checkpoints"
related_code:
  - "src/onoda/store.py"
  - "src/onoda/canonical.py"
  - "src/onoda/records.py"
  - "src/onoda/forks.py"
  - "src/onoda/controller.py"
  - "pi/extensions/lib/spool.ts"
  - "pi/rlm/onoda_rlm_kernel.py"
  - "pi/extensions/onoda-rlm.ts"
carries: [A10, A21, A22, A23, A24, A36, A37, A43]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor immutable-single-writer-records -->
## Records are append-only, written by one writer, and never depend on live process state

A committed object or record is never overwritten; a reanalysis writes a new record that cites the old one. Bytes are committed before the records and journal events that cite them, by one writer per run. Canonical records hold no floats. A run is restarted or forked only from a declared, quiescent checkpoint, never from a live kernel, REPL or process.

**Why.** A run record is the portable, forkable artifact this project exists to produce (docs/handoff/BUILD_SPEC.md). If a record could change after it was cited, or depend on a float's binary formatting or a kernel's memory, a fork or replay on another machine would silently disagree with the original.

### Invariant
- `Store.put_record` refuses a second record with the same id and different content; `put_bytes` never rewrites an existing object.
- `Store.append_event` refuses an event that cites an uncommitted object or record (bytes, then records, then the event).
- `canonical_bytes` raises `CanonicalizationError` on any float and on ints beyond 2^53-1; exact values are decimal strings with units.
- One authoritative writer per run: no locking exists, so nothing may append to a run's journal concurrently. Pi runs write only a spool; the store is written by `ingest` after Pi exits.
- A spec digest is not an execution id: identical specs produce distinct `InvocationReceipt`s.
- Forks start only at a journaled checkpoint whose boundary is in `records.BOUNDARIES`; a fork never inherits outputs of a changed node or anything after its visibility cut.
- There is no native resume: `Controller.resume` accepts only `mode="semantic"` on checkpoints that declare `semantic_restart: supported`.
- RLM kernel state enters the record only through `commit_state` (JSON, never pickle), spooled before the kernel is acknowledged.

### Known gaps (not yet enforced; owner to decide)
- A36: forking is a feature, and Pi runs cannot be forked yet (owner, 2026-10-04: build it, do not refuse it). `fork_run` handles fixture runs only; for a Pi run it fails incidentally on Pi step keys, and no test covers that. Pi checkpoints already carry the observation-pack map, compactions and mechanism config a fork will need.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

Drift found when this was verified against code (2026-10-04):

- A24 cites "AGENTS.md rule 6"; that rule is `docs/handoff/AGENTS.md` rule 6 and names framework sessions, pickle, VM snapshots and vector databases rather than a REPL, subagent or daemon. The behaviour (no native resume) is unchanged.
- Journals are not strictly write-once: import may replace a local journal with a longer bundle journal it is a prefix of, and `recover()` truncates a torn tail into quarantine. Both only extend or repair, never rewrite committed events.

#### A10: Native Prime/Pi session portability

Register status: assumed: not portable. Owner milestone: M1.

Implement portable semantic checkpoints independent of any running kernel; label native persistence/session features separately as harness-local conveniences.

#### A21: Can canonical records carry floating-point numbers?

Register status: decided. Owner milestone: M1.

No. Floats are banned from canonical JSON (`onoda.canonical.canonical_bytes` raises `CanonicalizationError`); exact decimals are carried as strings. See `tests/test_core.py::test_floats_and_unsafe_ints_rejected`.

#### A22: Can more than one process append to a run's journal?

Register status: decided. Owner milestone: M1.

One authoritative writer per run. The store's commit order (object → record → event) and hash-chained journal assume a single writer; concurrent writers are out of scope until an explicit locking design is added.

#### A23: Can a fork be taken from any point in a running trajectory?

Register status: decided. Owner milestone: M1.

No. Forks require a quiescent, declared checkpoint boundary (`initialized`, `job-collected`, `diagnostics-committed`, `decision-committed-before-submission`, `finalized` — see `onoda.records.BOUNDARIES`); a fork mid-step is not supported.

#### A24: Does resuming a run replay the original process/kernel state?

Register status: decided. Owner milestone: M1.

No native resume. Only semantic restart from a serialized checkpoint is supported; a persistent REPL, subagent, or daemon is never the canonical record (AGENTS.md rule 6).

#### A36: Fork and resume of Pi runs

Register status: decided for M3. Owner milestone: M3.

Not supported yet: Pi-run checkpoints declare `semantic_restart: unsupported`. They carry the observation-pack handle map, compaction events, and mechanism config so a later milestone can add restart without re-recording.

#### A37: When do Pi tool outputs enter the store?

Register status: decided. Owner milestone: M3.

The Pi process writes an fsynced spool (objects before the lines that cite them) before any byte reaches the model; the store is written only by `ingest` after the process exits, keeping one writer per run (A22). A spool without `spool.end` ingests as an incomplete run.

#### A43: Can a checkpoint depend on RLM kernel memory?

Register status: decided. Owner milestone: M4.

No (SOL_PI_DELTA §3). Anything a later step relies on goes through `commit_state(name, value)`: JSON only (never pickle), spooled as `rlm.state` before the kernel is acknowledged, ingested as an artifact, and cited from checkpoints under `pi.rlm_state`. The kernel's namespace is a harness-local convenience that restart never restores.
