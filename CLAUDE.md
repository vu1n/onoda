# onoda — agent instructions

Portable, forkable run records for scientific-agent experiments. Python
package `onoda` (CLI `onoda`). The design contract is `docs/handoff/`;
`docs/handoff/SOL_PI_DELTA.md` overrides conflicting text there (Pi is the
first agent adapter; Prime is an optional comparison arm).

## Toolchain

- Install: `uv sync`
- Test: `uv run pytest` (all suites are offline; no credentials, network, or solver)
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .` (check with `--check`)
- Demo: `scripts/demo.sh` runs the full clean-workspace sequence
- CI: `.github/workflows/ci.yml` runs lint, format check, the Pi typecheck and
  the full test suite (Pi installed, so `requires_pi` suites run) on every PR
- Pi runtime (M3): `scripts/pi-setup.sh` installs the pinned Pi + SoL-Pi from
  `pi/package-lock.json` once (network). Suites marked `requires_pi` launch
  that real Pi offline under a scripted provider and skip without it.
  TypeScript check: `cd pi && npx tsc -p .`
- Ablation (M3b): `onoda ablation run spec.yaml` runs arms × cases on the real
  Pi; fixture spec in `tests/ablation_fixtures/`.
- RLM and optimizer (M4): `pi/extensions/onoda-rlm.ts` plus the stdlib-only
  kernel `pi/rlm/onoda_rlm_kernel.py` make variant S1r; `onoda optimize
  seed|split|run|status` runs GEPA (pinned `gepa==0.1.4`) over
  `HarnessCandidate` components; `onoda optimize compare` is the live gate
  check that promotes a candidate to seed (A54).
- QUESTDB slice (VTE): `onoda pi run-vte <case>` runs an agent that writes its
  own ORCA inputs; `orca_submit` reaches the host broker
  (`science/orca_broker.py`) over a socket in the Pi work dir, so keep
  `--work-dir` short (AF_UNIX, 104 bytes on macOS). `onoda pi evaluate-vte`
  grades by state identity, `onoda vte report` summarizes arms; each
  broker reply carries advisory ladder checks (`science/orca_checks.py`, A66, A68)
  that nothing grades; `onoda vte shadow` records what a System One model
  (Jev, Clef) would decide per job, beside the grade (A67);
  `experiments/questdb/run.sh` is the S0-vs-S1 experiment on the Mac. Cases
  are `cases-dev/questdb/*/*.vte.yaml`; their `references.json` is
  evaluator-only.
- Scientific cases: `cases/**/*.case.yaml` pin authentic outputs by sha256;
  `onoda case list` shows them. Vendored bytes under `cases/` are never edited.
- Dev-only cases: `cases-dev/` (cclib-data logs, no upstream license) for
  held-out checks via `--cases-root cases-dev`; never part of `cases/` or a split.

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
- Acquired solver outputs are recorded as acquired (`executed_here: false`),
  never as executions; stand-in executables are labelled and their
  observations are synthetic.
- Parsed values are the printed decimal text with units and line evidence;
  anything unreadable is an explicit problem, never a default. Evaluation
  never matches states by nearest energy, and a missing tolerance makes a
  comparison `unresolved`, not `pass`.
- Every live-execution setting defaults off and unresolved live settings fail
  closed.
- Pi runs: every byte the model sees is spooled (fsynced) before Pi hands it
  over, the store is written only by ingest, the recorder loads last, fused
  `then_run` never reaches a solver, scripted runs are labelled synthetic, and
  the evaluator grades claims from the deterministic parser, never from
  reducer receipts.
- Ablations: reports live in the evaluator store only; a capability metric
  without a declared tolerance is `unresolved`; the false-success tolerance is
  zero; cases outside `development`/`acceptance` splits are refused; reports
  over scripted runs say they are synthetic and not a mechanism verdict.
- VTE runs: ORCA runs only in the host broker, never in the sandbox; its
  journal (outside the Pi work dir) is committed with the run and ingest
  refuses a spool that disagrees with it; a cache hit or a replayed job is
  acquired (`executed_here: false`), never an execution; the bundle's,
  gateway's and broker's `max_executions` are one number; a staged bundle
  holding the references is refused before anything runs; claims are graded
  from the parser over the cited job's bytes and paired with references by
  (spin, irrep, character), never by energy order.
- Shadow decisions (A67): live calls default off; the state holds agent-visible
  data only; answers go to the evaluator store, are never shown to the agent and
  never read by the grader or gates.
- RLM: kernel state enters the record only through `commit_state` (JSON,
  spooled as the exact bytes before the kernel gets its digest); every child
  Pi run is ingested as its own run before the parent cites it, and its
  requests and cost are charged to the parent's budget and metrics.
- Optimizer: trials run on acceptance cases only, never the final split;
  reflection sees agent traces plus verdict words and reason classes (A53),
  never reducer receipts or evaluator references; a candidate is accepted on an
  `eligible` gate verdict or, with `capability_gain` (A52), on a passing
  capability gate with a strict capability gain, so an unresolved capability
  gate never accepts; candidates change only the allowlisted components (A45).
  The seed changes only by promotion (A54): `onoda optimize compare` passes
  the gates against the current seed, and `seed_candidate.json` holds exactly
  the bytes it measured.

Read `docs/ASSUMPTIONS.md` before changing scientific behavior, and record
new assumptions there instead of inventing thresholds or API methods.
