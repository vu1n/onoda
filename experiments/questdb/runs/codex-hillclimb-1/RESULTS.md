# QUESTDB memory hill climb 1

Stopped after **three consecutive rejected candidates**, the prescribed stopping
rule. The final incumbent is unchanged **`memory-project@7`, 22/56 pooled mean**
(historical 23, fresh seed 21). Candidates @8, @9 and @10 scored **19, 16 and
20/56**. None passed screening; no confirmation or held-out run was warranted.

Across the 32 live runs, false-success claims and audit false accepts were zero.
All runs settled; there were no provider errors or two-hour timeouts. The seed
had one run without a claim; every candidate run had a claim.

Latest conservative budget charge: **at most $4.587421003**, below the $15
limit. Exact actual spend is unresolved for the reasons below. Results are
recorded on `codex/memory-hillclimb-1`; no new failure-only PR is opened,
as requested. Main was not edited.

## Batch ledger

Counts are target slots identified and within 0.3 eV by grader v3. Each batch is
S1 only, four repeats each for ketene and thioacetone, 56 slots. Billing deltas
are actual key-counter measurements; attribution to this task is unresolved.

| Candidate / batch | Commit | Version | Change | Ketene x4 | Thioacetone x4 | Total | Decision | False successes | Audit false accepts | No claim | Spend / billing measurement | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 / seed-0 | 79d1f775c01987238783dcfd33b8809abb7dcd9b | @7 | unchanged | 4,4,2,0 | 3,0,4,4 | 21/56 | seed: calibration passed | 0 | 0 | 1 | actual unresolved; upper bound $2.074485044 | 2h20m33s |
| 1 / candidate-1 | e58bf29c09ce1a52f272d30e8632b2ca818a7f13 | @8 | upfront CI-backed identity/reporting workflow | 2,0,0,2 | 3,3,4,5 | 19/56 | reject: total and ketene guards | 0 | 0 | 0 | billed-key delta $0.898924500; attribution unresolved | 1h30m32s |
| 2 / candidate-2 | f549a09ba0f4b2fcee85659722f8fb2c8b3b1622 | @9 | upfront physical-orbital/group plan | 0,0,2,2 | 3,2,4,3 | 16/56 | reject: total and ketene guards | 0 | 0 | 0 | billed-key delta $0.880268391; attribution unresolved | 1h59m02s |
| 3 / candidate-3 | f394a1e46b08ecd93cbcdab842100f7c7ca6e7fe | @10 | upfront converged-CI provenance check | 4,0,2,2 | 3,1,4,4 | 20/56 | reject: total and ketene guards; stop | 0 | 0 | 0 | billed-key delta $0.61819449; attribution unresolved | 2h02m51s |

The seed ledger row uses `batch_id`, `memory_version`, `per_run` and
`runs_without_claim`; candidate rows use `batch`, `memory`, `runs` and `no_claim`.
The original appended records are preserved; their values were checked against
the archived scores, commit pins and staged memory versions.

The historical per-run counts were verified from v3 verdict metrics and saved
in `historical-seed.json`: ketene 4,1,2,2; thioacetone 4,3,4,3. Historical source
blocks scored 12/28 (gate 6) and 11/28 (noise 1). Combined historical score 23/56
and fresh seed 21/56 give mean **22/56**, range 21–23, spread 2. The fresh seed
differed by 2, below the calibration stop threshold of more than 6.

Incumbent molecule means were ketene **9.5/28**, thioacetone **12.5/28**.
Screening required at least **27/56**, ketene at least **8.5/28** and thioacetone
at least **11.5/28**, zero false successes/false accepts and at most one run
without a claim. Confirmation would have required at least **25/56** plus the
same guards, frozen before candidate data. Every candidate failed total and
ketene guards. MAE never influenced a decision.

The seed's `S1-thioacetone-r2` stopped after one-root preparation j0005 without
`report_vtes`; it was settled, not a provider error, and was not rerun. No paid
agent run was repeated.

## Preflight, validation and provenance

Base: `79d1f775c01987238783dcfd33b8809abb7dcd9b`. Main remains at `1329f72`
with its pre-existing untracked `.mcp.json` untouched. No root repository
`AGENTS.md` exists; the supplied global instructions apply.

The first attempt stopped before spending because `.pt` was absent (17 passed,
3 skipped, 101 setup errors). The user requested retry and no failure-only PR;
PR #45 was closed. Creating the missing parent directory allowed the exact
prescribed Mac preflight to pass **118 tests, 3 skipped, 30.10 s** in detached
`/Users/vuln/code/onoda/.pinned/79d1f77`. Sandbox profiles and tests were unchanged.
That was the single successful sandbox preflight used for the search.

Each candidate's existing memory staging/leakage boundary tests passed (2 tests)
before its commit; diff whitespace checks passed. Each detached pin ran
`uv sync`, matched main's `pi/package-lock.json`, and received main's existing
`pi/node_modules`. Model, reference data, task inputs, ladder, grader, normalizer,
parser, decision model and run caps were unchanged. Non-secret settings and
commit pins are retained in each batch's `launch.json`. Fresh work roots were
`/tmp/codex-hc-0` through `/tmp/codex-hc-3`, detached under `caffeinate -i` with
200 requests, $1 Pi estimate cap, 7200 s timeout, four repeats and four workers.

Automatic approval review initially blocked paid transfer. The user explicitly
approved the original scope: structures, spin/irrep counts, guidance, ladder
and generated ORCA outputs to DeepSeek via OpenRouter under $15. No additional
paid reflection or decision-model calls were made.

One diagnostic inadvertently displayed reference-label keys in grader verdicts.
They were not forwarded to model inputs or encoded in guidance. Subsequent
verdict reads whitelisted numeric metrics. Candidate 3's procedure came from
agents' own transcript/output sections and contains no occupation strings,
target-specific character, energy or state ordering.

Durable archives under `seed-0/`, `candidate-1/`, `candidate-2/` and
`candidate-3/` retain inputs/outputs, bundles, sessions, broker journals,
agent/evaluator stores and text workspace artifacts. Credential files, caches
and large ORCA scratch products are excluded. Original work roots remain.
Archive manifests document selections. Raw stores and billing counters remain
local; only this report and ledger are committed as results.
The local archive root is
`/Users/vuln/Documents/Codex/2026-10-08/files-pasted-by-the-user-codex/work/onoda/experiments/questdb/runs/codex-hillclimb-1/`.

## Iteration evidence and uptake

No bullet was accepted, so accepted-bullet uptake counts are not applicable.
The following notes explain the rejected experiments; they describe agent
behavior, not chemistry findings.

- **@8, CI/identity reporting:** CI analysis was visible in all eight transcripts,
  but final reports still usually cited one space per spin. Thioacetone r1 used
  a separate B2-targeted j0008 after valence j0005/j0006. Thioacetone r2 planned
  diffuse acceptors but saw them leave the active window in j0005–j0007.
- **@9, physical-orbital planning:** ketene r2 cited separate valence/B1 target
  jobs j0013–j0016, and r3 cited j0011/j0012/j0016/j0017. Ketene r1 split by
  triplet irrep while retaining one CAS; r4 planned groups then returned to
  all-target jobs. Thioacetone r2 and r4 used separate additional-target jobs;
  r1 and r3 returned to all-singlet averages. Some grouping uptake improved,
  but the score fell.
- **@10, final-CI provenance:** ketene r1 (j0006/j0007) and r3 (j0016/j0017),
  and thioacetone r1 (j0006/j0007), visibly discussed CAS-SCF state blocks or
  final expansions. Thioacetone r2 still explicitly inspected the initial CI
  check before reporting j0009/j0010. Naming and physical space-selection
  problems remained; the batch did not pass either required score guard.

The specific @10 evidence was `codex-hc-2 S1-thioacetone-r3 j0007`: the transcript
decoded `INITIAL CI STATE CHECK` at archived scheduler
`candidate-2/jobs/job-00080/job.out` lines 955–984. Its converged
`CAS-SCF STATES FOR BLOCK` sections at lines 1764–1803 follow the convergence
signal and have different dominant occupations. This motivated using the final
section with its corresponding orbital ordering, without encoding a target answer.

Candidate 1 had a separate environment problem: Python ignored its installed
`onoda.pth` file with a recurring macOS `UF_HIDDEN` flag in the Documents pin.
Four settled thioacetone stores, the pooled report and the audit were recovered
with the same pinned source and explicit `PYTHONPATH=<pin>/src`; no agent was
rerun. Original errors and `done.json` remain alongside recovery records. The
recovered full audit returned 0 and false accepts 0. Its baseline count 22 grades
rows separately; the batch slot score 19 includes duplicate-identity handling.
Candidates 2 and 3 used the original checkout's short `.pinned/<sha>` location;
run script, report and audit all returned 0 without recovery.

## Final incumbent full diff against @7

**Empty.** The final `experiments/questdb/memory/memory-project.md` was restored
byte-for-byte from the base commit's @7 file. No rejected candidate is retained
in the final guidance. Candidate commits and pinned worktrees preserve each
experiment for review.

## Held-out results

Not run: no candidate was accepted, so the protocol's held-out condition was
not reached. The original 4-repeat S1 arithmetic yields 16 runs while its text
also says 32; clarification was requested, but this ambiguity had no effect on
this stopped search. No held-out data was used to tune guidance.

## Proposals for Vu

- Review the two bare-`p` identity labels identified in the supplied task and
  wrong-state dig. Seed `S1-ketene-r2` reported dedicated triplet A1 j0014 as
  `pi,pi*`, which the grader did not identify. No label, reference, normalizer,
  grading contract or task-input change was made.
- Consider exposing the converged CI section and its ordered active-MO mapping
  in host observations. The initial/final sections in candidate 2's thioacetone
  r3 j0007, and continued initial-CI reading in candidate 3's thioacetone r2,
  show why text guidance alone can be missed. This would require a separately
  reviewed parser/observation change; none was implemented here.
- Retain provider-reported billed cost separately from Pi's price-table estimate.
  Pi's `parseChunkUsage` calls `calculateCost` rather than retaining OpenRouter's
  billed `usage.cost`; logged generation-ID accounting lookups returned HTTP
  404. The key counter works, but the seed's first snapshot was after launch.
  SDK/runtime/accounting changes are outside this task.

## Spend and stop reason

Exact actual task spend is unresolved: the seed's first billed-key snapshot
was after launch, and key exclusivity was not confirmed. The pre-snapshot
usage was 306,655 input, 942,010 cache-read and 74,945 output tokens. Charging all
input/cache reads at the maximum listed input rate ($0.44/M) and output at
$1.32/M bounds startup spend at **$0.64834000**. The later billed-key increase
and inter-batch gaps are included in the cumulative budget charge, along with
any unrelated use of the key. Pi estimates were not treated as actual billing.

At closing snapshot 2026-10-08T22:48:47.836681+00:00, the conservative charge was
**$4.587421003**, leaving at least **$10.412578997**
under the $15 budget. Billed-key deltas per candidate are in the table/ledger;
exact per-run billed costs were unavailable. This is a conservative charge,
not an assertion of exact task spend.

**Stop reason: three consecutive rejected candidates after the seed.**
Three candidate batches were scored, zero accepted. No confirmation was
warranted; no provider rerun was needed; no held-out check was triggered.

MAE footnote: fresh seed pooled MAE was 0.603926 eV. Candidate pooled and per-run
MAEs remain in their archived reports/score records; MAE was ignored for every
decision.
