> Historical amendment record. Commands and paths below describe the retired
> workflow; they are not current instructions.

# Amendment proposal: pi-pinned

**Decision:** doc://onoda/pi-first-pinned-stack@0001#pi-pinned
**Proposed by:** brief v0.4.2 upgrade on PR #26   **Date:** 2026-10-04

## What should change
Replace the "Known gaps" bullet with a sentence in the decision: "`build_launch` refuses to stage a run when the installed Pi version or SoL-Pi commit differs from the pins (`check_pins`); a missing install is left to fail at launch." Drop the empty "Known gaps" heading.

## Why
The decision is right; its "Known gaps" text is now out of date. Fixed on this PR: `adapters/pi/runtime.py` adds `check_pins` (file reads only) and calls it from `build_launch`, so `run_info`'s pinned `pi_version` and `sol_pi_commit` are what actually ran. Test: `test_a_run_refuses_a_pi_or_sol_pi_off_the_pins`.

## Code that needs it
None. The code already conforms to the decision; only the gap note is stale.

## Impact / risk
Text only. Ratifying publishes revision 0002; re-pin the refs (`brief pin`) and run `brief doctor`.


---
ratified_rev: 0002
ratified_by: vu1n
