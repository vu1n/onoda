# Converged CASSCF observation: interrupted comparison

The observation change is implemented and reviewed in draft PR #46. The live
comparison is **incomplete**, not a negative accuracy result. A network/DNS
failure made the added billed-counter monitor unavailable; its fail-closed
guard stopped the batch. No accuracy conclusion against 22/56 is warranted.

Experiment pin: `4e8469d5d1112ddec8f13960f055b9c340c14d22`, based on
`79d1f775c01987238783dcfd33b8809abb7dcd9b`. `memory-project@7` is byte-for-byte
unchanged, as are the authoritative parser, grader, references, target inputs,
ladder, model and run caps. The pre-spend plan is
[`../../converged-states-1.md`](../../converged-states-1.md).

## Run status

S1 only, ketene and thioacetone, four repeats each. Started
2026-10-09T02:23:32.446358Z; the worker recorded its stop at
2026-10-09T03:19:41.809350Z. Fresh work root: `/tmp/codex-obs-1`.

| Run | Status | Within 0.3 eV | Next eligible action |
|---|---|---:|---|
| ketene r1 | provider-error: connection error | 0/7 recorded | one provider-error retry allowed |
| ketene r2 | guard-aborted | not scored | restart requires exception |
| ketene r3 | settled | 0/7 | retain |
| ketene r4 | settled | 4/7 | retain |
| thioacetone r1 | guard-aborted | not scored | restart requires exception |
| thioacetone r2 | guard-aborted | not scored | restart requires exception |
| thioacetone r3 | guard-aborted | not scored | restart requires exception |
| thioacetone r4 | not started | not scored | first attempt remains authorized |

The two settled runs total **4/14**. The third ingested run ended in a provider
error, so it is eligible for the protocol's single replacement. The three
ingested stores have zero false-success claims; their audit returned zero false
accepts. A complete eight-run audit has not been performed. Thioacetone r2/r3
also recorded connection errors, but their terminal spool status is `aborted`;
they have not been silently reclassified to bypass the no-other-reruns rule.

The user decision requested is an exception to restart the four guard-aborted
runs at the same pinned code and settings, retaining both settled results,
then complete the provider-error retry and the one unstarted run. It is pending.
No paid retry or replacement has been launched.

## Observation delivery

All **40 converged jobs** in the interrupted attempt had final CI states and a
complete ordered final active-MO map in their broker summaries: ketene
r1/r2/r3/r4 contributed 14/8/10/5, thioacetone r1/r2 contributed 1/2.
These summaries also appear in agent-visible tool results. No mapping was
unavailable. This verifies delivery, not correct interpretation or accuracy.

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

Before this attempt, billed-key usage was `95.374323144`. The last successful
monitor snapshot before the interruption was `95.830024335` at 02:52:48Z.
The post-stop accounting request also failed with a DNS resolution error.
One free read-only recovery request succeeded at 03:24:28.831480Z, reporting
`95.883650573`.

Thus the recovered billed-key increase is **$0.509327429**. Added to the prior
conservative charge of $4.587421003, the cumulative charge is
**$5.096748432**, leaving **$9.903251568** under the original $15 authorization.
This includes any simultaneous use of the key; exact task attribution remains
unresolved. Pi estimates are not reported as billed cost. The $13 spending
threshold was not reached; the stop was loss of accounting connectivity.

All batch processes were confirmed stopped. `batch-1/` preserves launch settings,
the original stop record, audit, billing monitor, recovered billing snapshot,
sessions, broker journals, stores and ORCA inputs/outputs (3,367 archived files).
Credentials, caches and large scratch products are excluded. The original
`/tmp/codex-obs-1` is retained. Only this report and the ledger are committed;
raw archives remain local.

The code PR remains a draft, unmerged. No reference-label, provider-accounting,
guidance, held-out or stronger-model changes were made. This is exploratory
development work, not a chemistry finding.
