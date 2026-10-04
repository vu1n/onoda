---
id: orca-parser-authoritative
project: onoda
type: decision
status: active
title: "The deterministic ORCA parser is the only source of graded values; ORCA's exit code is not trusted"
related_code:
  - "src/onoda/science/orca_parser.py"
  - "src/onoda/science/contract.py"
  - "src/onoda/science/orca_local.py"
  - "src/onoda/ablation/metrics.py"
carries: [A15, A27, A28, A29, A56]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor parser-is-authoritative -->
## Grade only what the deterministic parser read from the output bytes

Every graded value comes from `onoda.science.orca_parser` over the raw output bytes: the printed decimal text with its printed unit and line evidence, never converted. Whether a job finished is the parser's termination check, never ORCA's exit code (ORCA exits 0 on failed runs). Reducer receipts, model summaries and decision models are never graded. A change to what the parser records bumps `PARSER_ID`; observations recorded under an older id keep it and stay readable.

**Why.** The parser is the one judge that cannot be talked into an answer. Exit codes and summaries have already been wrong in this project (ORCA exit 0 on a failed CASSCF, see `tests/fixtures/orca611-nevpt2/README.md`).

### Invariant
- Parsed values are `Quantity(value: str, units, evidence)`; an unreadable or missing value is an explicit `Problem`, never a default.
- Missing `****ORCA TERMINATED NORMALLY****` makes the parsed listings `unresolved`.
- Units are validated against printed tokens and never converted; `FINAL SINGLE POINT ENERGY` is recorded as `Eh` by ORCA's convention (A27).
- Multiplicity is never inferred where ORCA does not print it (A28, A56).
- `PARSER_ID` is `orca-parser@3` (adds `casscf_converged` and `mr_states`); nothing compares a stored observation's parser id to the current one.
- The evaluator never reads reducer receipts; `reducer.evidence_lost` measures what receipts drop (A15).

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

Drift found when this was verified against code (2026-10-04):

- A56 says the parser is `orca-parser@2`. Code is `orca-parser@3` (`src/onoda/science/orca_parser.py:72`): @3 added SA-CASSCF/SC-NEVPT2 transition listings (`mr_states`) and `casscf_converged` for the QUESTDB slice (PLAN.md VTE-005). No register row recorded the @3 bump; this doc does now.
- `src/onoda/science/runner.py:9` docstring said `orca-parser@2`; corrected to `@3` with this migration.

#### A15: Reducer completeness on scientific logs (does a compact receipt drop a convergence warning or root flip?)

Register status: unresolved (probe built, no model data). Owner milestone: M3b.

The deterministic `chemistry` parser stays authoritative; the evaluator never reads reducer receipts; measure diagnostics lost in receipts on dev cases before trusting them. M3b reports `reducer.evidence_lost` per run: parser evidence lines in a reduced raw result that are absent from what the model saw. Scripted receipts say nothing about a real reducer.

#### A27: Units of `FINAL SINGLE POINT ENERGY`

Register status: assumed. Owner milestone: M2.

ORCA prints no unit token on that line; the parser records `Eh` by ORCA's documented convention. State lines carry printed unit tokens (`au`, `eV`, `cm**-1`), which are validated, never converted.

#### A28: Multiplicity of EOM-CCSD / ADC(2) roots

Register status: decided. Owner milestone: M2.

Not printed on `IROOT` lines, so it stays unknown (`None`) and state identification is `unresolved` for those cases. It is not inferred from `DoTriplet false` in the input.

#### A29: Which cclib validates the parser?

Register status: assumed. Owner milestone: M2.

`cclib==1.9rc1`, dev-only, used by `tests/test_orca_parser.py` as an independent cross-check. The latest stable release (1.8.1) cannot read the ORCA 6 files. Not a runtime dependency.

#### A56: `<S**2>` on MDCI `IROOT` lines

Register status: decided. Owner milestone: M2.

ORCA 6.1.1 ends STEOM-CCSD state lines with `<S**2> = value` (first seen executing `orca6-dvb-steom-ccsd` locally; the vendored 6.0.1 log has no such suffix). The parser checks the value is a fixed-point number and otherwise ignores it, as it does on TD-DFT `STATE` lines: it is not recorded and multiplicity is still not inferred from it (A28). Any other trailing text stays an `error`. This is `orca-parser@2`: `orca-parser@1` reported such a line as an `error`, so the id was bumped (Vu, 2026-10-03) and observations already recorded under `@1` keep that id and stay readable; nothing compares an observation's `parser` to the current id.
