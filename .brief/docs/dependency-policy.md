---
id: dependency-policy
project: onoda
type: decision
status: draft
title: "No DSPy dependency; single-author packages are allowed"
related_code: []
---

<!-- brief:anchor no-dspy-dependency -->
## Do not add DSPy as a dependency

onoda has its own RLM extension (`pi/extensions/onoda-rlm.ts` with `pi/rlm/onoda_rlm_kernel.py`),
so DSPy's RLM is not a dependency. Today no DSPy package is in `pyproject.toml`, `uv.lock` or
`pi/package-lock.json`; DSPy appears only as a prompt reference (`provenance/sources.json` S18).
Packages maintained by a single author are allowed (owner, 2026-10-04).

**Why.** The RLM extension is built around onoda's semantic checkpoints; a second RLM stack would
duplicate it with a different state model.

### Invariant
- No `dspy` in Python or npm dependencies.
