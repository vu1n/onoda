---
id: evaluator-isolation
project: onoda
type: decision
status: active
title: "Evaluator references and credentials never reach the agent store or an agent-visible export"
related_code:
  - "src/onoda/store.py"
  - "src/onoda/portability.py"
  - "src/onoda/science/evaluator.py"
  - "src/onoda/adapters/pi/run.py"
carries: [A3, A12]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor references-never-agent-visible -->
## Keep references and credentials out of everything the agent can see

Evaluator answers (case references, QUESTDB `references.json`) live only in the evaluator store. A staged agent bundle that holds them is refused before anything runs. Export refuses non-agent records and scans for secrets; import is data-only (no code execution, pickle, network or installs).

**Why.** A reference value that leaks into agent-visible bytes makes every later grade meaningless, and a bundle that ran code on import would make sharing records unsafe.

### Invariant
- `Store.put_bytes` raises `VisibilityError` for evaluator bytes in an agent store; evaluation records are written to `evaluator_store` only.
- `_refuse_references` refuses a staged VTE bundle containing the references (by pin sha256 or schema string).
- `export` refuses non-agent records and `_check_leaks` rejects secrets and protected bytes; `import_bundle` rejects symlinks and hardlinks and runs nothing.

### Known gaps (not yet enforced; owner to decide)
- `Store.put_record` itself has no visibility check; evaluation records stay out of the agent store because every evaluator writes to `evaluator_store`, not because the store refuses them.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

#### A3: Original prompts and evaluator

Register status: unresolved (paper's code/data section is a placeholder). Owner milestone: M2.

Reconstruct conservatively; label as reconstruction; keep evaluator answers outside agent-visible storage (BUILD_SPEC §5).

#### A12: Original authors' private communications

Register status: assumed: out of scope. Owner milestone: M0.

Do not export private messages or infer scientific instructions that were not provided in this packet.
