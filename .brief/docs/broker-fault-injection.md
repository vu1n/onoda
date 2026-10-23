---
id: broker-fault-injection
project: onoda
type: decision
status: draft
title: "An opt-in fault plan makes the broker serve a known failure in place of a job's output, recorded as injected and never credited"
related_code:
  - "src/onoda/science/orca_broker.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "src/onoda/adapters/pi/vte.py"
  - "src/onoda/adapters/pi/run.py"
  - "experiments/questdb/run.sh"
---

<!-- brief:anchor injected-faults -->
## Test whether an agent recovers from a known failure, without lying in the record

A run may carry a **fault plan**. When a submission matches one of the plan's triggers, the broker
still runs the job as usual. It then serves the agent a **fault output** derived from the job's real
output by a named, versioned transform, in place of the real output. The agent sees an ordinary
`executed` job that failed. The broker journal records both outputs, the fault applied and its
transform version. Ingest stores both, and the injected output is labelled `fault-injected` and
`synthetic`. Recovery is then measured per fault: did the run still report an accurate state for
the targets the faulted job served, at what extra cost in jobs and requests, and did the agent name
the failure?

**Why.** The ladder (rules A to H) exists to make an agent recover from failed jobs. No experiment
so far could show whether it does. Suite 1's S0-vs-S1 gap was inside its run-to-run spread, and
real failures are rare and uncontrolled: Opus 5.5 had 9 failed jobs of 93 and never hit the
24-job cap. The Nature Reviews Methods Primer *Agentic AI systems for scientific discovery*
(doi 10.1038/s43586-026-00534-y, Results) names fault injection as the test strategy for
recovery. With Opus 5.5 now repeating its counts exactly across runs, a planted fault is a
controlled measurement for the first time: the same fault at the same point in every run, and
a clean baseline to compare against.

**Why execute, then substitute.** The agent must not be able to tell an injected failure from a
real one, so its reply must say `executed`. If the broker skipped ORCA, the record would have to
say "not executed" while the agent was shown "executed", and ingest's spool-against-journal check
exists precisely to stop that kind of disagreement. Running the job keeps `executed_here` true.
Only the served bytes differ, and the journal says so.

### Fault catalogue (version 1)

Every v1 transform removes information from the real output. None adds or edits a printed
number. Every v1 transform cuts the output before ORCA's NEVPT2 transition table, so under
`vte-grading-contract` a claim citing an injected job can never be credited. No grading change is
needed.

| Fault id | Transform of the real output | What it imitates | Ladder rule it exercises |
|---|---|---|---|
| `killed-before-nevpt2` | Keep everything before the first NEVPT2 section header; status `failed`, reason `injected` | A job killed at the wall time after CASSCF | A (job failure) |
| `killed-mid-casscf` | Keep everything up to and including the k-th CASSCF macro-iteration (k from the plan, default 3); status `failed` | A CASSCF that ran out of time unconverged | C (CASSCF non-convergence) |

Fault kinds that need a real wrong-state output (rule D) or missing roots (rule E) are out of
scope for v1. They would substitute a different, recorded ORCA output, which needs pinned per-case
fault sources from the Mac run stores. That is a later revision of this decision.

### The fault plan

- A YAML file given with `--fault-plan`, plus `FAULT_PLAN=` in `run.sh`. It is off by default
  (`live-off-by-default`), and the run config pins its sha256.
- Each trigger names a fault id and a deterministic match. v1 offers two matches: the n-th
  submission that got a job, or the first job whose input is a CASSCF or NEVPT2 calculation
  (read from the `!` line).
- Each trigger fires at most once per run. An identical resubmission of a faulted input (same
  cache key) is served the same fault output again, so a resubmission is not a free fix. It is
  still counted as a job.
- A run with a plan is labelled `fault-injected`. Its arm is reported as `<variant>+<plan id>`,
  never pooled with clean runs of the same variant.

### Invariant

- The broker runs every faulted job for real. The reply's `executed_here`, `cache_hit` and
  `status` describe the served output exactly as the agent saw it.
- The journal reply entry carries `injected: {fault, transform_version, plan_sha256}` and both
  `output_sha256` (served) and `real_output_sha256`. Ingest checks the spool against the served
  output, as today, and stores the real output as a second object of the same receipt.
- An injected output is never written to the shared cache. The job's real output is not cached
  either, so a later clean run can't be served bytes a faulted run produced.
- Every faulted job counts toward `max_executions` (`orca-broker-boundary`).
- Every v1 transform is a pure prefix cut of the real output at a parsed marker. If the marker is
  absent, the trigger does not fire, and the journal records `injected: skipped` with the reason.
- Replay serves the recorded served bytes like any other journaled reply (`replay-fails-closed`).
  A fork inherits the parent's plan unless the fork declaration changes it, which is a declared
  fork change.
- The `orca_checks` and `summary` lines are computed from the served output, as the agent would see
  them for a real failure of that kind.
- `vte report` adds per-fault recovery fields for `fault-injected` arms: accurate count on the
  faulted job's targets, jobs and requests after the fault, and whether the claim notes or report
  text mention the failure. These are descriptive, like the process metrics.

### Open questions for the owner (decide before this goes active)

1. Is execute-then-substitute acceptable? The alternative is to skip ORCA and show the agent
   "executed" anyway, which saves the job's wall time but needs an amendment to ingest's
   agreement check.
2. Should the real output of a faulted job be cached for other runs (it is a genuine ORCA result),
   or kept out (the default above, for isolation)?
3. Is "the agent named the failure" worth measuring? It needs a text match on the agent's report,
   with the false positives that brings.
4. Which first experiment: `killed-before-nevpt2` on the first NEVPT2 job, ketene and thioacetone,
   S0 vs S1, Opus 5.5, 4 repeats each (16 runs, about $25)?
