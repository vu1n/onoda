# QUESTDB memory hill climb 1

In progress. Seed calibration passed; incumbent is unchanged `memory-project@7`
at **22/56** pooled mean (historical 23, fresh 21). Candidate 1 (`@8`) tests an
upfront CI-backed root-identity and reporting workflow; it has not been scored.

## Batch ledger

Counts are identified target slots within 0.3 eV. Each batch has 56 slots,
S1 only, four repeats each for ketene and thioacetone.

| Candidate / batch | Commit | Version | Change | Ketene x4 | Thioacetone x4 | Total | Decision | False successes | Audit false accepts | No claim | Actual spend | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 / seed-0 | 79d1f775c01987238783dcfd33b8809abb7dcd9b | @7 | unchanged | 4,4,2,0 | 3,0,4,4 | 21/56 | seed: calibration passed | 0 | 0 | 1 | unresolved; upper bound $2.074485044 | 2h20m33s |

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

Durable copies retain inputs, outputs, bundles, sessions, broker journals and
agent/evaluator stores under `seed-0/`. Large ORCA scratch products and caches
were omitted from the archive; the original work directory is still present.

## Final incumbent diff against @7

Empty so far: no candidate has been accepted. Candidate 1's working-tree
proposal changes only the version and Reporting section of the project memory.

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

Exact seed spend is unresolved. Billed-key usage increased **$1.426145044** after
the first snapshot. The pre-snapshot token counts give a conservative startup
bound **$0.64834000**, using maximum listed provider token
rates and charging cache reads at the maximum uncached rate. Thus the current
budget charge is **at most $2.074485044**, including any other use of that key.
The key-exclusivity question is pending. These figures are not Pi's estimated
cost and are not reported as exact task spend. Future batches capture key
counters before launch and after completion.

No stopping condition has been reached. Candidate batches launched: 0; accepts: 0.

MAE footnote: fresh seed pooled MAE is
0.603926 eV; ignored for decisions.
