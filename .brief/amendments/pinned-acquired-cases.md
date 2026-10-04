# Amendment proposal: pinned-acquired-cases

**Decision:** doc://onoda/case-data-provenance@0001#pinned-acquired-cases
**Proposed by:** brief v0.4.2 upgrade on PR #26   **Date:** 2026-10-04

## What should change
Delete the "Known gaps" bullet saying only `orca-smoke` evaluations carry `case.review`. Keep the bullet about byte immutability.

## Why
The decision is right; its "Known gaps" text is now out of date. Fixed on this PR: `pi-claims` (`adapters/pi/task.py`) and `vte-claims` (`adapters/pi/vte.py`) evaluations now record `case.review` from the case manifest, so all three evaluators meet A26. Tests assert it in `test_pi_task.py` and `test_pi_vte_grading.py`.

## Code that needs it
None. The code already conforms to the decision; only the gap note is stale.

## Impact / risk
Text only. Ratifying publishes revision 0002; re-pin the refs (`brief pin`) and run `brief doctor`.
