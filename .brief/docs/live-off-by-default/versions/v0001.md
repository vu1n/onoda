---
id: live-off-by-default
project: onoda
type: decision
status: active
title: "Every live setting defaults off, needs explicit values, and fails closed"
related_code:
  - "src/onoda/science/orca_local.py"
  - "src/onoda/adapters/pi/runtime.py"
  - "src/onoda/systemone.py"
  - "src/onoda/records.py"
  - "experiments/questdb/run.sh"
carries: [A7, A9, A11, A20, A35]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor live-settings-explicit -->
## Nothing live runs unless every setting is given explicitly

ORCA execution, live model providers and live decision-model calls are off by default. Each needs an explicit enable flag plus every setting (binary, work root and wall time for ORCA; provider, model, credential variable name, `max_requests` and `max_cost_usd` for Pi). Nothing is inferred from a present API key, and an unresolved setting refuses the run. Simulated costs are labelled simulated.

**Why.** Live runs spend money and run an operator's licensed solver; a default that turned them on, or a present key that implied consent, would spend without a decision.

### Invariant
- `OrcaSettings.resolve` raises `CapabilityError` unless enabled with binary, work root and wall time.
- `LiveSettings.validate` needs every field and the named credential variable; a live run without a sandbox is refused.
- `LiveSystemOne` raises unless `enabled=True`; CLI enable flags are `store_true`.
- `experiments/questdb/run.sh` exits without `ENABLE_LIVE_EXECUTION=1` and every provider setting.
- Fixture and mock receipts carry `Usage.simulated=True`.

### Known gaps (not yet enforced; owner to decide)
- A9: the recorder spools Pi's normalized assistant message, not raw provider bytes or headers, so the served checkpoint is recorded only as far as Pi surfaces it; no record states that limit yet.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

Drift found when this was verified against code (2026-10-04):

- `budgets.summarize` always reports `simulated: true`, even over real receipts (over-labels, never under-labels).
- `LiveSystemOne` falls back to a per-provider default model once enabled; the enable flag itself still defaults off.

#### A7: Solver (ORCA) availability and redistribution rights

Register status: unresolved for execution (no ORCA in this environment); outputs: see A25. Owner milestone: M2.

Support user-provided installation only; verify permission before distributing any solver artifact. `onoda.science.orca_local` runs only an operator-supplied binary and is disabled by default.

#### A9: Provider-resolved model version (e.g. which underlying checkpoint served a request)

Register status: unresolved (may not be exposed by the provider). Owner milestone: M1.

Record whatever identifiers are observable in the response and state the resulting reproducibility limit; never infer an unexposed version.

#### A11: API credentials, paid budget, and infrastructure authorization

Register status: assumed: none authorized. Owner milestone: M0 / M1.

Offline defaults everywhere; live execution requires explicit operator configuration, credentials, and budget (never inferred from a present API key).

#### A20: How are compute/solver costs represented before a real solver is integrated?

Register status: assumed. Owner milestone: M1.

Fixture costs are simulated numbers, never presented as real ORCA/provider spend; every simulated value is labeled as a fixture (AGENTS.md rule 11).

#### A35: Live Pi runs (a real provider)

Register status: assumed: none authorized (A11). Owner milestone: M3.

Every live setting (provider, model, credential variable name, `max_requests`, `max_cost_usd`) must be set explicitly; there are no defaults and nothing is inferred from a present API key. `max_cost_usd` is checked against Pi's own cost estimate, which is an estimate. A live run also needs the sandbox (A50); without one it is refused.
