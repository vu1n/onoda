# QUESTDB memory hill climb 1

In progress. Seed calibration passed; incumbent remains unchanged
`memory-project@7` at **22/56** pooled mean (historical 23, fresh 21).
Candidate 1 (`@8`) was rejected at **19/56**: ketene **4/28** and thioacetone
**15/28**. It failed the 27/56 screen threshold and ketene's 8.5/28 molecule
minimum. All eight runs settled with claims; no provider errors or timeouts.
False successes and audit false accepts are both zero. Consecutive rejects: 1.

Candidate 2 (`@9`) is prepared from the unchanged @7 incumbent. Its single
intervention is an upfront physical-orbital inventory and target-group plan,
with selection checked after re-reading and state averaging. It removes the
one-extra-orbital heuristic; the rejected @8 reporting changes are excluded.
Both existing memory boundary tests passed (2 passed, 0.04 s) using the exact
branch source via an explicit Python import path. Diff whitespace check passed.

## Batch ledger

Counts are identified target slots within 0.3 eV. Each batch has 56 slots,
S1 only, four repeats each for ketene and thioacetone.

| Candidate / batch | Commit | Version | Change | Ketene x4 | Thioacetone x4 | Total | Decision | False successes | Audit false accepts | No claim | Actual spend | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 / seed-0 | 79d1f775c01987238783dcfd33b8809abb7dcd9b | @7 | unchanged | 4,4,2,0 | 3,0,4,4 | 21/56 | seed: calibration passed | 0 | 0 | 1 | unresolved; upper bound $2.074485044 | 2h20m33s |
| 1 / candidate-1 | e58bf29c09ce1a52f272d30e8632b2ca818a7f13 | @8 | upfront CI-backed identity/reporting | 2,0,0,2 | 3,3,4,5 | 19/56 | reject: total and ketene guards | 0 | 0 | 0 | billed-key delta $0.898924500; attribution unresolved | 1h30m32s |

Seed pooled mean: `(23+21)/2 = 22/56`. Molecule means: ketene **9.5/28**,
thioacetone **12.5/28**. The two comparable 56-slot blocks are 23 and 21,
range 21–23 (spread 2). Historical source blocks separately scored 12/28
(gate 6) and 11/28 (noise 1). Historical per-run counts were verified directly
from v3 verdict records and are saved in `historical-seed.json`.

The settled run without a claim is `S1-thioacetone-r2`. It ended after the
one-root preparation job j0005 without a `report_vtes` call. It was not a
provider error and was not rerun. There were no provider errors or timeouts.

## Frozen scoring and acceptance

Use the pooled incumbent mean. A first candidate batch needs at least **27/56**;
confirmation needs at least **25/56**. Per-molecule, false-success, audit and
missing-claim guards apply to each batch. Confirmation does not count toward
the eight-candidate cap. MAE does not determine any decision.

## Preflight and provenance

Base: `79d1f775c01987238783dcfd33b8809abb7dcd9b`. The main checkout remains at
`1329f72`, with its existing untracked `.mcp.json` untouched. No root `AGENTS.md`
exists in the repository; the supplied global instructions apply.

The first attempt stopped before spending because `.pt` was absent (17 passed,
3 skipped, 101 setup errors). The user authorized retry and requested no
failure-only PR; PR #45 was closed. After creating `.pt`, the prescribed Mac
preflight passed **118 tests, 3 skipped, in 30.10 s** in detached
`/Users/vuln/code/onoda/.pinned/79d1f77`. Sandbox profiles and tests were unchanged.

Automatic approval review initially blocked the paid launch. The user then
explicitly approved the original transfer of molecular structures, spin/irrep
counts, guidance, ladder and generated ORCA outputs to DeepSeek via OpenRouter
under the original $15 budget. The seed ran detached under `caffeinate -i` at
fresh `/tmp/codex-hc-0`, with the prescribed model, caps and reminder. Its
non-secret settings and commit are in `seed-0/launch.json`.

Durable copies retain inputs, outputs, bundles, sessions, broker journals,
text workspace artifacts and agent/evaluator stores under `seed-0/`. Credential
files, large ORCA scratch products and caches were omitted from the archive;
the original work directory is still present.

## Candidate 1 interpretation and grading recovery

The CI/reporting workflow was visible in all eight transcripts, but most final
reports still used one active space per spin rather than per acceptor group.
`S1-thioacetone-r1` did use a separate B2-targeted job (j0008 after valence
j0005/j0006). `S1-thioacetone-r2` planned diffuse acceptors but saw them move
outside the active window during preparation (j0005–j0007). Both observations
support testing an earlier, explicit physical-orbital plan next.

Four thioacetone agents settled but their grading CLI could not import `onoda`:
Python ignored the installed `.pth` file with macOS's recurring `UF_HIDDEN`
flag. Their existing stores, the full report and audit were recovered using the
same pinned source with `PYTHONPATH=<pin>/src`. No model run was repeated. The
original `done.json`, errors, and recovery records are preserved. Audit returned
0 with false accepts 0. Its baseline count 22 grades each row in isolation;
the full-report slot score is 19, which accounts for duplicate identities.

One diagnostic inadvertently displayed reference-label keys in grader verdicts.
They were not forwarded to model inputs or encoded in guidance. Subsequent
verdict reads whitelist numeric metrics; candidate 2 will concern the general
physical-orbital planning procedure, based on agents' own inputs and transcripts.
Future pins will use the original checkout's short `.pinned/<sha>` location,
as the successful seed did.

## Final incumbent diff against @7

Empty so far: no candidate has been accepted. Candidate 1's rejected
proposal changed only the version and Reporting section of the project memory.
Its two existing memory boundary tests passed; the diff whitespace check passed.

## Uptake notes

Seed evidence used for candidate 1: `S1-ketene-r4` chose a space partly to obtain
unique character strings, then scored 0; `S1-ketene-r2` used a donor index
(`pi2`) as character; `S1-ketene-r3` corrected inconsistent singlet/triplet
CI labels; `S1-thioacetone-r2` stopped before any target table or claim. These
are observed agent behaviors, not chemistry findings. Accepted-bullet uptake
will be counted only if a candidate is accepted.

## Held-out results

Not run. The task specifies two molecules × four S1 repeats × two versions,
which is 16 runs, but also says 32. A clarification was requested before any
held-out spending. No held-out result will be used to edit the memory.

## Proposals for Vu

- Investigate the two bare-`p` identity labels mentioned in the task and
  wrong-state dig. Fresh `S1-ketene-r2` reported a dedicated triplet A1 job
  j0014 as `pi,pi*`, which the grader did not identify. No label, normalizer,
  reference or task-input change has been implemented.
- Retain provider-reported billed cost independently of Pi's price-table
  estimate. Pi's `parseChunkUsage` calls `calculateCost` instead of preserving
  OpenRouter's billed `usage.cost`; all early logged response-ID lookups to the
  generation accounting API returned HTTP 404. The current key's billed counter
  works, but the seed's first snapshot was taken after launch. Runtime/SDK
  changes are outside this task.

## Spend and stop reason

Exact task spend is unresolved: the seed's first billed-key snapshot was taken
after launch, and key exclusivity has not been confirmed. Candidate 1's billed
key delta was **$0.898924500**. Across the task, the current conservative budget
charge is **at most $2.983178083** (startup bound $0.64834000 plus billed-key
increase since the first snapshot, including inter-batch gaps and other use).
At least **$12.016821917** remains under the $15 limit. These are billed-key
measurements and a conservative bound, not Pi's price-table estimate.

No stopping condition has been reached. Candidate batches scored: 1; accepts: 0;
consecutive rejects: 1. No confirmation was warranted.

MAE footnote: fresh seed pooled MAE 0.603926 eV; candidate 1 MAE is retained in
its report and per-run score records. MAE is ignored for decisions.
