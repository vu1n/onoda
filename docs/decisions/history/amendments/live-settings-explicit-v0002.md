> Historical amendment record. Commands and paths below describe the retired
> workflow; they are not current instructions.

# Amendment proposal: live-settings-explicit

**Decision:** doc://onoda/live-off-by-default@0001#live-settings-explicit
**Proposed by:** brief v0.4.2 upgrade on PR #26   **Date:** 2026-10-04

## What should change
Add `signoff: required` to the frontmatter of `.brief/docs/live-off-by-default.md`. No change to the decision's text.

## Why
brief v0.3.0 made sign-off opt-in: after the upgrade to v0.4.2, a decision without the field no longer asks for a `conforms` line when its governed code changes. brief's UPGRADING.md says to mark only the decisions where silent drift is dangerous (security boundaries, data loss, money). Every live setting spends money or reaches a provider; a default that drifts on would spend without an operator's explicit word.

## Code that needs it
None. Without this, changes under this decision's `Context:` refs land with no sign-off ask.

## Impact / risk
A PR that touches code under this decision's `Context:` refs needs a `.brief/SIGNOFF` line. Ratifying publishes revision 0002; re-pin the refs (`brief pin`) and run `brief doctor`.


---
ratified_rev: 0002
ratified_by: vu1n
