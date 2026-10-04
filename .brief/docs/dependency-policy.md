---
id: dependency-policy
project: onoda
type: decision
status: draft
title: No DSPy dependency and no third-party single-author packages in the harness
related_code: []
---

<!-- brief:anchor no-single-author-deps -->
## Do not add DSPy or third-party single-author packages as dependencies

**Draft: not verifiable from the repository.** This convention comes from the owner's instructions outside the repo. It needs the owner's confirmation before it is promoted to `active`. Rejected options so far: DSPy RLM as a dependency (onoda has its own RLM extension), and third-party packages maintained by one author. Today no DSPy package is in `pyproject.toml`, `uv.lock` or `pi/package-lock.json`; DSPy appears only as a prompt reference (sources.json S18).

**Why.** Every dependency of the harness is part of what a reproduced run trusts; small single-author packages are the likeliest to vanish or change under a pin.

### Invariant
- No `dspy` in Python or npm dependencies.
- Open question for the owner: current deps include `typebox` (effectively single-maintainer), `gepa` and `rfc8785` (small teams). Are these accepted exceptions, or does the rule mean something narrower?
