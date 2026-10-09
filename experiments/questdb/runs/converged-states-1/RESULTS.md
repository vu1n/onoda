# Converged CASSCF observation: completed comparison

The authorized eight-run cohort scored **11/56**, versus the unchanged seed
mean **22/56** (range 21–23). Ketene scored **4/28** versus 9.5/28; thioacetone
scored **7/28** versus 12.5/28. This batch provides no evidence of an accuracy
improvement: the pooled loss of 11 exceeds the original four-slot noise rule.
Two runs reached their two-hour caps without claims and count as zero.
This is a single development batch, not a confirmed effect or chemistry finding.

The complete pooled audit returned **zero false accepts**. All eight evaluations
recorded **zero false-success claims**. Six runs settled normally and two timed
out. The observation was delivered in all 58 converged jobs, so delivery alone
did not improve this model's score under these fixed resource limits.

Experiment pin: `4e8469d5d1112ddec8f13960f055b9c340c14d22`, based on
`79d1f775c01987238783dcfd33b8809abb7dcd9b`. `memory-project@7` is byte-for-byte
unchanged, as are the authoritative parser, grader, references, target inputs,
ladder, model and run caps. The pre-spend plan is
[`../../converged-states-1.md`](../../converged-states-1.md).

## Selected results

S1, DeepSeek v4 Flash through OpenRouter; four repeats per molecule, seven slots
per run. Grader v3, tolerance 0.3 eV. Selection was fixed before the continuation
results: retain the two settled original runs and replace the other six as
authorized. No result was selected by score.

| Run | Selected attempt | Terminal status | Within 0.3 eV |
|---|---|---|---:|
| ketene r1 | restart-1; one provider-error retry | settled | 0/7 |
| ketene r2 | restart-1; approved guard restart | settled | 0/7 |
| ketene r3 | batch-1; retained | settled | 0/7 |
| ketene r4 | batch-1; retained | settled | 4/7 |
| thioacetone r1 | restart-1; approved guard restart | settled | 4/7 |
| thioacetone r2 | restart-1; approved guard restart | timeout; no claim | 0/7 |
| thioacetone r3 | restart-1; approved guard restart | timeout; no claim | 0/7 |
| thioacetone r4 | restart-1; first attempt | settled | 3/7 |

The two timeouts have `complete: false` and `pi_exit_code: -1`, with no final
claim. They are counted in the denominator and were not repeated. An ordinary
ORCA job timeout does not authorize a new agent run. After Pi's run cap, broker
shutdown waits for an already-running ORCA job to finish or hit its own cap;
this explains thioacetone r2's later terminal-record time.

## Recovery provenance

The original attempt started 2026-10-09T02:23:32.446358Z. A network/DNS failure
made billed-counter monitoring unavailable; the fail-closed guard stopped its
processes, recording completion at 03:19:41.809350Z. Ketene r3/r4 had settled,
ketene r1 ended in provider-error, four runs were guard-aborted, and thioacetone
r4 had not started. The original partial result was not an accuracy comparison.

The user then explicitly approved the exception with “ok, re run it”. The
continuation ran from 03:37:23.155029Z to 06:01:16.593632Z (143.9 minutes), using
the same pin and settings in fresh `/tmp/codex-obs-1r`. It retained the two
settled results, used ketene r1's single provider-error retry, restarted exactly
the four guard-aborted IDs, and ran thioacetone r4 for the first time. Its worker
and audit exited zero, with no guard stop or further provider-error replacement.
The original interruption and all discarded attempts remain in the archive.

Both the final `vte audit` and `vte report` covered exactly the eight selected
stores at the experiment pin and exited zero. The first local audit invocation
was blocked by outer-sandbox access to the uv cache; the host-permission rerun
passed. The evaluator and repository sandbox were unchanged.

## Observation delivery

All **58 converged jobs** in the selected runs contained final CI states and a
complete ordered final active-MO map in their broker summaries, also present in
agent-visible tool results. Counts by run were ketene 5/13/10/5 and thioacetone
7/3/8/7. No mapping was unavailable. The original interrupted attempt separately
had 40 converged jobs with complete observations; those are not added to the
selected-cohort count. Delivery does not establish correct interpretation.

The extractor exposes printed indices, symmetry labels, occupations, leading
configurations and line evidence. Chemical character is not assigned. Initial
CI checks are excluded, and missing or unsupported final evidence is explicit.

## Validation and review

- Focused state/summary/parser/check tests passed; final added boundary cases
  passed (31 state/summary tests).
- Full local suite: 936 passed, 12 skipped under the outer sandbox; its 63
  socket/Seatbelt-blocked checks all passed with host permissions. Repository
  sandbox controls were not changed.
- Pinned Mac preflight: **118 passed, 3 skipped**, 18.02 seconds, before spend.
- Ruff lint/format, Pi typecheck, diff check and brief check passed. Brief doctor
  reported no warnings and two pre-existing draft-decision informational notes.
- GitHub CI and governance checks passed for the experiment commit.
- Offline application to all 311 converged jobs in the prior search found final
  CI and complete MO maps in every job; the largest summary was 3,563 characters.
- Tighten and independent structure/correctness/security review completed.
  One low-confidence ROOT-text-to-Kypp-argv candidate was independently dropped
  against the final numeric-only ROOT grammar. Zero unresolved findings.

## Billing and preservation

The billed-key counter moved from `95.374323144` before the original attempt
to `96.455438775` at 2026-10-09T06:01:45.749254+00:00. The original attempt accounted for
$0.509327429; the continuation counter increase was **$0.571788202**.
The total observed increase for this observation experiment, including recovery,
was **$1.081115631**. Added to the prior conservative memory-search charge of
$4.587421003, cumulative accounting is **$5.668536634 of $15**, leaving
**$9.331463366**. The $13 monitor threshold was not reached.

These counters include any concurrent use of the configured key; exact task
attribution remains unknown, and Pi price-table estimates are not billed costs.
The final free counter read succeeded after all model runs had terminated.

`batch-1/` preserves 3,367 archived files and `restart-1/` preserves 5,157,
including settings, sessions, broker journals, stores and ORCA inputs/outputs.
`selected-runs.json` fixes provenance; final audit/report, delivery, score and
billing snapshots remain alongside them. Credentials, caches and large scratch
products are excluded. Both original work roots are retained. Final process inspection found no
remaining evaluation or ORCA processes; main remains at `1329f725` with its
pre-existing untracked `.mcp.json`. Only this report and the ledger are committed;
raw archives remain local.

Draft PR #46 remains unmerged. No guidance, reference-label, provider-accounting,
confirmation, held-out or stronger-model changes or calls were made. The
observation implementation passed its boundary tests, but this batch does not
justify adopting it as an accuracy improvement for DeepSeek v4 Flash.
