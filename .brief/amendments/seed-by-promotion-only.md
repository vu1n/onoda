# Amendment proposal: seed-by-promotion-only

**Decision:** doc://onoda/optimizer-scope@0002#seed-by-promotion-only
**Proposed by:** Claude, from the eval-design and hill-climbing review (project thread, 2026-10-08)   **Date:** 2026-10-08

## What should change
Add a validation slice to `SplitAssignment` (acceptance / validation / final). GEPA reflects on and gates minibatches from acceptance as today; a proposal the gates accept is then run on the validation slice and kept only if it also passes `gate_decision` there against its parent. Train-only gains are recorded `rejected` with the reason "acceptance gain did not hold on validation". The final slice stays untouched until one check at the end. The invariant "Trials run on the acceptance slice only" becomes "Trials run on the acceptance and validation slices only; the final slice is never run during a search".

## Why
Today a candidate is accepted on the five-case acceptance slice and held-out behaviour is checked by hand afterwards (live3, heldout-tddft, heldout-other). A second split that every accepted proposal must also pass is the standard guard against the climb fitting the acceptance cases. Using the final slice for this each round would be adaptive evaluation (EXPERIMENT_PLAN §5), so it needs its own slice.

## Code that needs it
`SplitAssignment` (schema bump to `onoda.split/2`), `OnodaAdapter.evaluate` (allow validation cases), the loop's acceptance step, `onoda optimize split` (a `--validation-count`), and tests. None of it is in the PR that adds this proposal.

## Impact / risk
Each accepted proposal costs one more validation-slice evaluation. Existing `onoda.split/1` artifacts stay readable as acceptance + final with no validation slice (the extra check is skipped and recorded as such). With few cases, the validation slice takes cases away from reflection; two or three cases is the practical minimum.
