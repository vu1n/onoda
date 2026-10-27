# QUESTDB agents carry frozen markdown memory tiers between runs; kypp runs only in shadow

**Status at migration:** draft. This migration does not change that status.


<a id="frozen-markdown-tiers"></a>
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
- kypp runs only in shadow (`onoda vte memory-shadow`): its answers go to the evaluator store as
  `vte-memory-shadow-kypp` evaluations and never reach the agent or the grader.
- The shadow seeds a fresh kypp database (an existing one is refused) with one claim per tier
  bullet plus one pitfall per failure seen in seed runs; every seed run's last journal event is
  earlier than every shadowed run's creation, or the shadow is refused.
- kypp is asked with what the agent could see after each job (its `!` lines and the parser
  summary); a failure counts as covered only when the recalled text, or the tiers' text, contains a
  phrase of its fix (`FIXES`), and the text sizes are recorded beside the counts.
- Failures come from the parser and from the grader's reason words (A53's limit); no reference
  value, reference key or error reaches kypp.
