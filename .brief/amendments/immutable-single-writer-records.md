# Amendment proposal: immutable-single-writer-records

**Decision:** doc://onoda/record-integrity@0002#immutable-single-writer-records
**Proposed by:** Pi run forking PR   **Date:** 2026-10-04

## What should change
1. Add `src/onoda/adapters/pi/fork.py` to `related_code`.
2. Add one invariant bullet after the fork bullet:
   "A Pi run is forked by re-running it (`adapters/pi/fork.py`): the child replays the parent's recorded responses up to the fork point, then a continuation answers: a script, or a live model the session hands off to after the replay (its prefix recorded as `replayed`, its own caps on top of the prefix). It inherits no receipt, every prefix tool call must show it the result it showed the parent (or the fork is refused), and it records a `ForkDeclaration`, `Run.parent`, `fork.created` and the visibility cut on every checkpoint. A VTE fork's broker replays the parent's jobs before the fork point and runs only later ones live."
3. Replace the A36 "Known gaps" bullet with:
   "A36: RLM runs and imported runs cannot be forked yet, nor can a live continuation of a variant with a reducer or of a prefix ending in a compaction summary. A replayed prefix job's files are attachable to a later job only when the shared broker cache holds them."

## Why
Vu asked for forking to be built rather than refused (2026-10-04). This PR builds it, so the gap note no longer describes the code.

## Code that needs it
None of the code depends on the new text. `adapters/pi/fork.py`, the broker's `prefix` mode, the fork arguments of `ingest_spool`, `run_case` and `run_vte_case`, the `onoda pi fork` command, and `fork_run` now pointing Pi runs to it all conform to the decision as written: a fork starts at a journaled, quiescent checkpoint and inherits nothing.

## Impact / risk
The text changes, and a new governed file is added. Ratifying publishes revision 0003. Re-pin the refs with `brief pin`, then run `brief doctor`.
