---
id: commit-attribution
project: onoda
type: decision
status: draft
title: Commits are authored as the owner with no AI attribution trailers
related_code: []
---

<!-- brief:anchor owner-authored-commits -->
## Author commits as the owner, with no co-author or generated-by trailers

**Draft: not verifiable from the repository.** This convention comes from the owner's instructions outside the repo. It needs the owner's confirmation before it is promoted to `active`. Commits on this repository are authored as `vu1n` with no `Co-Authored-By` or session-link trailers, and PR descriptions carry no generated-by footer or attribution block, because the owner plans to open-source the repository.

**Why.** Attribution lines would become part of the public history once the repository is opened.

### Invariant
- Every commit's author is the owner; no commit message carries an attribution trailer.
