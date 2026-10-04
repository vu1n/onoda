---
id: memory-tiers
project: onoda
type: decision
status: draft
title: "QUESTDB agents carry frozen markdown memory tiers between runs; kypp runs only in shadow"
related_code:
  - "experiments/questdb/memory/*.md"
  - "experiments/questdb/run.sh"
  - "tests/test_questdb_memory.py"
carries: [A75]  # the A75 row this decision replaces in the former register
---

<!-- brief:anchor frozen-markdown-tiers -->
## Give both arms the same frozen markdown memory tiers

Owner, 2026-10-04: "markdown tiers as a baseline and kypp as a shadow eval". Two tiers in
`experiments/questdb/memory/` reach the agent as allowed documentation, so each run stages and pins
the exact bytes it saw: `memory-harness.md` (how the tools behave, for any molecule) and
`memory-project.md` (ORCA 6.1.1 procedures and pitfalls for this project). `run.sh` passes them by
default; `MEMORY_DIR=none` runs without memory.

**Why.** Lessons from earlier runs (energies subtracted across jobs are rejected, ORCA's default
orbital step can fail without an error, a disk search finds nothing) lived only in notes, so every
run started from nothing. A live, shared store would change a run's inputs while an experiment is
running and let runs teach each other mid-experiment.

### Invariant
- Every entry names the job or run it came from.
- Entries are procedures and pitfalls only: never a value for a target state, a reference energy, a
  reference active space or a reference state average (`tests/test_questdb_memory.py` checks the
  files against every `references.json`).
- Entries come from agent-visible records and onoda's own host runs, never from the evaluator store.
- A tier changes only by a commit between experiments, so all runs of one experiment see the same
  bytes.
- Both arms get the same tiers, so S0 vs S1 still isolates the ladder; whether memory helps is its
  own comparison.
- kypp runs only in shadow (planned, not built): its answers never reach the agent or the grader,
  and its store is fed only from runs that finished before the run it is asked about.
