> Historical amendment record. Commands and paths below describe the retired
> workflow; they are not current instructions.

# Amendment proposal: seed-by-promotion-only

**Decision:** doc://onoda/optimizer-scope@0001#seed-by-promotion-only
**Proposed by:** brief v0.4.2 upgrade on PR #26   **Date:** 2026-10-04

## What should change
Delete the two "Known gaps" bullets (A51 seed affordability ignores `--repeats`; A54 `compare_candidates` counts unknown cost as $0) and the empty "Known gaps" heading.

## Why
The decision is right; its "Known gaps" text is now out of date. Both are fixed on this PR. `optimize/loop.py` sizes the seed evaluation check as cases × repeats for both the trial and the spend budget. Under a spend cap, `optimize/compare.py` charges a failed run its reserve and stops on a run that reports no cost, rejecting the candidate, as a live ablation does. Tests: `test_the_seed_evaluation_check_counts_repeats`, `test_a_failed_run_is_charged_its_reserve_under_a_cap`, `test_an_unknown_cost_stops_the_check_and_never_promotes`.

## Code that needs it
None. The code already conforms to the decision; only the gap note is stale.

## Impact / risk
Text only. Ratifying publishes revision 0002; re-pin the refs (`brief pin`) and run `brief doctor`.


---
ratified_rev: 0002
ratified_by: vu1n
