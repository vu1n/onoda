---
id: broker-fault-injection
project: onoda
type: decision
status: draft
title: "An opt-in fault plan makes the broker serve a known failure in place of one job's output, recorded as injected, never credited, and measured against clean controls"
related_code:
  - "src/onoda/science/orca_broker.py"
  - "src/onoda/adapters/pi/ingest.py"
  - "src/onoda/adapters/pi/vte.py"
  - "src/onoda/adapters/pi/run.py"
  - "experiments/questdb/run.sh"
---

<!-- brief:anchor injected-faults -->
## Test whether an agent recovers from a known failure, without lying in the record

A run may carry a **fault plan**. When a submission matches the plan's trigger, the broker still
runs the job as usual. It then serves the agent a **fault output** in place of the real output:
the real output cut by a named, versioned transform. This is observation substitution, not a
simulation of a resource kill, because the full job's real cost is still paid. The agent sees an
ordinary `executed` job that failed. The broker journal records both outputs, which fault was
applied and the transform version. Ingest stores both, and the served output is labelled
`fault-injected` (not `synthetic`; see the invariants).

Recovery is measured against **clean controls**: the same variant and case, run at the same commit
with no plan. The question is whether the ladder reduces the damage a fault does. An experiment
made only of faulted runs measures performance under stress, and can't separate "S1 recovers
better" from "S1 does better anyway".

**Why.** The ladder (rules A to H) exists to make an agent recover from failed jobs, and no
experiment so far could show whether it does. Suite 1's S0-vs-S1 gap was inside its run-to-run
spread. Real failures are rare and uncontrolled: Opus 5.5 had 9 failed jobs out of 93 and never
hit the 24-job cap. The Nature Reviews Methods Primer *Agentic AI systems for scientific discovery*
(doi 10.1038/s43586-026-00534-y, Results) names fault injection as the way to test recovery. Opus
5.5 now repeats its counts exactly across runs, so for the first time a planted fault is a
controlled measurement: the same fault at the same point, against a clean baseline.

**Why execute, then substitute.** The agent must not be able to tell an injected failure from a
real one, so its reply must say `executed`. If the broker skipped ORCA, the record would say "not
executed" while the agent was shown "executed", and ingest's spool-against-journal check exists to
stop exactly that disagreement. Running the job keeps `executed_here` true. Only the served bytes
differ, and the journal says so.

**Why so small.** This revision is the pilot. It ships one transient fault with one trigger, and
measures recovery per arm against clean controls. Per-job target attribution, more fault kinds,
ordinal triggers and forks of faulted runs are later revisions, added once the pilot shows an
effect worth attributing.

### The fault (version 1)

| Fault id | Transform of the real output | What it imitates | Ladder rule it exercises |
|---|---|---|---|
| `killed-before-nevpt2` | Keep everything before the first NEVPT2 section header. Status `failed`, reason `timeout` | A job killed at the wall time after CASSCF | A (job failure) |

The transform only removes information. It adds or edits no printed number, and it cuts the output
before ORCA's NEVPT2 transition table. So under `vte-grading-contract` a claim citing the injected
job can never be credited, and no grading change is needed.

The agent-facing `status` and `reason` are exactly what the broker sends for a real wall-time kill
(`timeout` is the reason `OrcaLocalScheduler` gives). The word "injected" appears only in the
journal and the record, never in a reply, because `orca_submit` shows the reason on the first line
of its result.

**The timing tell.** A real wall-time kill arrives at the wall limit. An injected one arrives
whenever the full job finished, which is often much sooner. v1 relies on the agent not knowing the
limit: `max_wall_seconds` is a broker setting that no bundle file, tool description or reply
states, and a job's `duration_ms` is a host-side journal fact the reply leaves out. A test pins
this down, and the prefix's own timings are a real run's, so they agree with
each other. If the limit ever becomes agent-visible, the faulted reply must be held until the limit
instead (open question 1).

### The fault plan

- The plan is a YAML file given with `--fault-plan`, plus `FAULT_PLAN=` in `run.sh`. It is off by
  default (`live-off-by-default`). The run config pins its sha256, and the plan's canonical bytes
  are committed to the run's store as an object the config cites.
- v1 has one trigger: the first job whose input requests an NEVPT2 calculation. The method is read
  from the whole input, not just the `!` line, because `orca_input` writes NEVPT2 as
  `PTMethod SC_NEVPT2` inside the `%casscf` block, as the authentic target inputs do. The match
  depends on the input text alone, and a test covers every input `orca_input` can build plus the
  fixture inputs.
- Only a single-point energy is eligible, and eligibility is a positive allowlist, not a list
  of exclusions: every `!` keyword must be on a pinned list of method, basis, auxiliary-basis,
  grid, convergence and print keywords (plus an explicit `SP` or `Energy`); every `%` block must be
  one of `%casscf`, `%scf`, `%pal`, `%maxcore` or `%output`, and within them nothing may set a run
  type other than an energy (a `%method` block, and so any `RunTyp`, makes the input ineligible);
  and there is no `$new_job`. Anything not on the list is ineligible, so a run type the list did
  not anticipate fails closed. Any other job could go on changing `job.gbw` after the cut (see the
  invariants). An ineligible NEVPT2 job runs and is served normally, and does not consume the
  trigger. The first eligible job may be a different input in S0 than in S1. A write-up says
  so.
- **The fault is transient.** The trigger fires once per run, on one submission. A later
  submission, including an identical resubmission, runs and is served normally (from the run's
  own cache if it is a hit), and counts as a job. A fault that follows the input on every
  resubmission would test escaping an input-specific fault, which favours a ladder that tells the
  agent to change inputs. That persistent condition is a separate, named fault for a later
  revision.
- A run whose plan fired is labelled `fault-injected`. Its arm is reported as
  `<variant>+<plan id>@<plan sha256 prefix>`, so it never pools with clean runs of the same variant,
  and an edited plan never pools with the old one. A run whose plan never fired (no NEVPT2
  submission) is reported separately as `...:not-fired`: neither a recovery nor a clean run.

### Invariant

- The broker runs every faulted job for real. The triggering submission skips the shared-cache
  lookup, so the fault always applies to a job this run executed, never to an acquired result. The
  reply's `executed_here`, `cache_hit` and `status` describe the served output exactly as the agent
  saw it.
- A fault applies only to a job that succeeded according to the deterministic parser
  (`orca-parser-authoritative`), not according to scheduler status: ORCA terminated normally and
  the CASSCF converged. If the real job timed out, failed or failed those checks, the trigger does
  not fire. The agent is served the real failure, and the journal records `injected: skipped` with
  the real reason, so a recovery is never credited to an injection layered over a genuine failure.
  If the NEVPT2 header is absent, the trigger is skipped the same way. A skip consumes the trigger:
  no later job is faulted, and the run is reported under `...:not-fired` with the skip reason, so a
  run never holds both an uncontrolled failure and a planted fault.
- The journal reply entry carries `injected: {fault, transform_version, plan_sha256}`, both
  `output_sha256` (served) and `real_output_sha256`, and the scheduler's `real_status` and
  `real_reason`. Ingest checks the spool against the served output, as it does today, and stores
  the real output as a second object of the same receipt.
- The served output is labelled `fault-injected`, never `synthetic`, `stand-in` or `not-orca`.
  Those labels mean "not a solver or model result", and `_vte_jobs` would turn them into a synthetic
  evaluation. A fault-injected run is a real agent on real ORCA with one declared, transformed
  observation. `vte report` states that ("fault-injected: real agent and ORCA, one served output
  transformed by plan <sha>") instead of the synthetic note. A run that is also scripted or
  stand-in stays synthetic, as today.
- A faulted reply offers what a real kill at that point would have left, as a real `timeout`
  reply does (the broker offers whatever files a failed job left behind). For
  `killed-before-nevpt2`, that is `job.gbw` and nothing else: the CASSCF has converged by then and
  NEVPT2 does not change the orbitals, so the real job's `job.gbw` is the file the imitated kill
  would have left. Every other product (`job_property.txt` above all, which can hold NEVPT2
  results) is withheld. Withholding `job.gbw` too would be its own tell and a different recovery
  path. The "NEVPT2 leaves `job.gbw` unchanged" premise holds only for the single-point inputs the
  trigger accepts, and it is an assumption to check before the pilot: compare the file from a full
  job against the one from a CASSCF-only run of the same input on the Mac, for each input shape
  `orca_input` builds for a single point. If they differ, the faulted reply offers no files and the doc says so. The journal
  records every product's sha256 either way.
- Neither the injected output nor the job's real output is written to the shared cache. The real
  output stays in the record for audit.
- The plan and the real output never reach the agent: not through a live tool, not through the Pi
  work dir or bundle, and not through resumed context. A test checks each route.
- The faulted job counts toward `max_executions` (`orca-broker-boundary`).
- Offline replay serves the recorded served bytes like any other journaled reply
  (`replay-fails-closed`), and a replayed entry keeps its `injected` field. v1 refuses a fork or
  restart of a fault-injected run.
- The `orca_checks` and `summary` lines are computed from the served output, as the agent would see
  them for a real failure of that kind.
- `vte report` reports a `fault-injected` arm beside its clean control. Per case, it shows the
  accurate count of each, and the jobs and requests the faulted runs spent after the fault. These
  are descriptive, like the process metrics, and never an arm verdict.

### Experiment protocol

- Every pilot run, faulted or clean, starts from a fresh empty cache, with that run's writes
  isolated. A preseed is not allowed in the pilot: a preseeded NEVPT2 job would be a cache hit in a
  clean control but executed in a faulted run, so the conditions would differ by more than the
  fault. Otherwise clean recovery jobs warm the
  cache for the next arm. Run order alternates arms and conditions rather than running all of S0
  before S1.
- First pilot: `killed-before-nevpt2`; ketene and thioacetone; S0 and S1; clean and faulted; Opus
  5.5; 2 repeats per cell. That is 16 runs, an estimated $25. Check the first block of 8 (one
  repeat per cell) for validity before running the second. It is a pilot, not broad evidence that
  the ladder works, and identical counts do not mean identical trajectories.
- Whether the agent names the failure is not measured automatically. A person reads each faulted
  run's first reply after the fault: does it recognise the interruption, see which evidence is
  missing, choose a fitting next step, and avoid claiming success it has no support for?
- Launching the pilot spends money and needs the owner's go-ahead. This decision does not give it.

### Later revisions (out of scope here)

- `killed-mid-casscf` (rule C), and wrong-state (rule D) or missing-roots (rule E) outputs, which
  need pinned per-case fault sources from the Mac run stores.
- The persistent fault that follows an input across resubmissions.
- Attribution of a faulted job's own targets (its `%casscf` root blocks, the case irrep mapping and
  the C2v B1/B2 convention), ordinal triggers, and forks of faulted runs with their trigger state.

### Open questions for the owner (decide before this goes active)

1. Is relying on the agent not knowing the wall limit enough, or should the faulted reply be held
   until `max_wall_seconds`? Holding costs up to 30 minutes per faulted run at the current
   setting.
2. Is the pilot design above (2×2×2×2, first block of 8 checked first) the one to run?
