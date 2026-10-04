---
id: match-by-identity
project: onoda
type: decision
status: active
title: "States are matched by identity, never by energy order; a missing tolerance is unresolved, not pass"
related_code:
  - "src/onoda/science/evaluator.py"
  - "src/onoda/adapters/pi/vte.py"
  - "src/onoda/science/vte.py"
carries: [A6, A30]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor no-energy-order-matching -->
## Never pair states by nearest energy, and never pass a comparison without a justified tolerance

Computed states are paired with references by declared identity (multiplicity and root rank for fixed cases; spin, irrep and character for VTE claims), never by nearest or ordered energy. A numeric comparison passes only against a tolerance whose `tolerance_source` names the decision (a `.brief/docs` decision, or a legacy A-row) that justified it; with no tolerance the comparison is `unresolved`.

**Why.** Energy-order matching makes a wrong state look right whenever the energies happen to line up, which is the failure the source paper warns about. An invented threshold would turn noise into a pass.

### Invariant
- `ComparisonPolicy` requires a `tolerance_source`; `_compare_quantity` with no tolerance yields `unresolved`.
- `orca-reexecution@1` carries no tolerance, so ORCA 6.0 vs 5.0 differences stay `unresolved` (A30).
- VTE `_candidates` never ranks references by energy.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

#### A6: Scientific state-matching / reproduction tolerances

Register status: unresolved. Owner milestone: M2.

No nearest-energy-only matching and no arbitrary universal threshold; tolerances require explicit, evidenced decisions (BUILD_SPEC §5).

#### A30: Reexecution tolerance between ORCA versions

Register status: unresolved (instance of A6). Owner milestone: M2.

`orca-reexecution@1` has no tolerance, so the ORCA 6.0 vs 5.0 comparison reports exact differences (e.g. 0.000006 au on the first singlet) and stays `unresolved`. A tolerance needs an assumptions-register row that justifies it.
