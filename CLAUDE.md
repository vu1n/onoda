# onoda — agent instructions

Portable, forkable run records for scientific-agent experiments. Python
package `labrun` (CLI `labrun`). The design contract is `docs/handoff/`;
`docs/handoff/SOL_PI_DELTA.md` overrides conflicting text there (Pi is the
first agent adapter; Prime is an optional comparison arm).

## Toolchain

- Install: `uv sync`
- Test: `uv run pytest` (all suites are offline; no credentials, network, or solver)
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .` (check with `--check`)
- Demo: `scripts/demo.sh` runs the full clean-workspace sequence

VCS is plain git. No project board; work items come from issues or goals.

## Invariants a review must verify

- Objects and records are immutable. Nothing overwrites a committed file; a
  migration or reanalysis writes a new record that cites the old one.
- Commit before visible: bytes, then records, then the journal event that
  references them. `Store.append_event` refuses uncommitted refs.
- No floats in canonical records; exact values are decimal strings with units.
- A specification digest is not an execution id. Identical specs produce
  distinct receipts.
- Offline replay fails closed (`ReplayMismatch`) on any request change; no
  nearest-match or live fallback.
- A fork never inherits outputs of a changed node or anything after its
  visibility cut, including via memory, search, or content-hash lookup.
- Evaluator references and credentials never enter the agent store or an
  agent-visible export.
- Import is data-only: no code execution, pickle, network, or installs.
- Synthetic fixture output is labelled synthetic and never described as a
  solver or chemistry result.
- Every live-execution setting defaults off and unresolved live settings fail
  closed.

Read `docs/ASSUMPTIONS.md` before changing scientific behavior, and record
new assumptions there instead of inventing thresholds or API methods.
