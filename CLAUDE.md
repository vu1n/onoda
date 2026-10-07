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
  grades by state identity, `onoda vte report` summarizes arms, `onoda vte audit` counts
  what the grader still credits when known-wrong variants of real claims are
  graded (a throwaway store; `label_swap` is measured, not failed); each
  broker reply carries advisory ladder checks (`science/orca_checks.py`, A66, A68)
  that nothing grades; `onoda vte shadow` records what a System One model
  (Jev, Clef) would decide per job, beside the grade (A67);
  `onoda decide tune` hill-climbs that question wording with GEPA against
  labelled examples (`onoda vte shadow-dataset` labels jobs from the grader, A70);
  `experiments/questdb/run.sh` is the S0-vs-S1 experiment on the Mac
  (`REPORT_REMINDER=1` / `--report-reminder` gives an agent that stops without
  `report_vtes` one hidden reminder, recorded in the run's config). Cases
  are `cases-dev/questdb/*/*.vte.yaml`; their `references.json` is
  evaluator-only.
- Scientific cases: `cases/**/*.case.yaml` pin authentic outputs by sha256;
  `onoda case list` shows them. Vendored bytes under `cases/` are never edited.
- Dev-only cases: `cases-dev/` (cclib-data logs, no upstream license) for
  held-out checks via `--cases-root cases-dev`; never part of `cases/` or a split.

VCS is plain git. No project board; work items come from issues or goals.

## Invariants a review must verify

Each invariant is a governed decision in `.brief/docs/` (read it with `brief resolve`);
this list is only the index. A review checks the change against every decision whose
`related_code` it touches.

- `record-integrity`: records are immutable, committed before visible (bytes, records,
  event), float-free, one writer per run; fork/restart only from semantic checkpoints.
- `replay-fails-closed`: offline replay matches exact digests or raises `ReplayMismatch`.
- `evaluator-isolation`: references and credentials never reach the agent store or an
  export; import is data-only.
- `orca-parser-authoritative`: graded values come only from the deterministic parser;
  ORCA's exit code is not trusted.
- `match-by-identity`: never pair states by energy; no tolerance means `unresolved`.
- `vte-grading-contract`: a claim counts only if the one cited job printed it, with irreps.
- `vte-task-inputs`: the agent gets structure and target counts, never energies.
- `live-off-by-default`: every live setting defaults off and fails closed.
- `agent-sandbox`: live agent commands run sandboxed or the run is refused.
- `orca-broker-boundary`: ORCA runs only in the host broker; one `max_executions`.
- `promotion-gates`: zero capability worsening; false-success tolerance is zero.
- `optimizer-scope`: allowlisted components only; the seed moves only by promotion.
- `decision-models-shadow-only`: Jev/Clef and ladder checks never grade.
- `pi-first-pinned-stack`: stock Pi 0.85.1 + SoL-Pi 1559b5c.
- `case-data-provenance`: case bytes pinned by sha256; acquired is never an execution.

Not yet captured as decisions (still check them):
- Synthetic fixture output is labelled synthetic and never described as a solver or
  chemistry result; stand-in executables are labelled and their observations synthetic.
- Pi runs: every byte the model sees is spooled (fsynced) before Pi hands it over, the
  recorder loads last, and fused `then_run` never reaches a solver.
- RLM: every child Pi run is ingested as its own run before the parent cites it.

Read the decisions in `.brief/docs/` before changing scientific behavior, and
record new assumptions there (as a draft decision) instead of inventing
thresholds or API methods. `docs/ASSUMPTIONS.md` only maps the old A-row ids
(cited throughout the code) to those decisions.

## Context Vault (brief)

Architectural decisions live in `.brief/docs/` as governance docs. Each has a stable
anchor (`<!-- brief:anchor id -->`), a `status`, and the code it governs (`related_code`
globs). A decision is addressable as `doc://<project>/<doc-id>@latest#<anchor>` — resolve
one with `brief resolve <ref>` to read the exact decision; don't infer it from the ref.

Decisions are **ratified constraints, not editable notes**. Develop *to* them:

- A decision with `status: active` is READ-ONLY to coding work. Do NOT edit it to make
  your change fit — your code conforms to the decision, not the reverse.
- If `brief check` asks for a sign-off (only decisions marked `signoff: required` do, and
  only when you touch the code under their `// Context:` comment) and your change STILL
  satisfies the decision, record `<anchor-id> conforms: <reason>` in `.brief/SIGNOFF`.
  `brief triage` (optional System One model) writes that line itself for the asks it
  rates as clearly conforming; read and answer the ones it leaves.
- If the task CANNOT be done without changing a ratified decision, you may NOT change it
  yourself. Write `.brief/amendments/<anchor-id>.md` (what should change and why), record
  `<anchor-id> amend-proposed: <reason>` in `.brief/SIGNOFF`, and STOP — it needs human
  ratification before code can land. Never bypass the commit hook.
- At each site that embodies a decision, leave a one-line comment:
  `// Context: doc://<project>/<doc-id>@latest#<anchor> — <the rule, in one line>`.
  The ref makes it checkable; the one-line rule puts the constraint in front of the next
  agent exactly where it touches the code. Keep the full reasoning in the decision doc.
- If your memory, notes, or habits disagree with an active decision, the decision wins:
  it is versioned and reviewed with the code, memory is not. Follow the decision and say
  which memory looked stale.

If the repo has a feature map (a `.brief/docs` doc with `type: features`), run
`brief features <files>` before changing code and read the Gotchas of each feature you
touch. When you move or delete code, update that feature's `paths:` in the same change;
when you hit a trap the next agent would hit too, add it to the feature's Gotchas.

**Before committing, run `brief check`** (resolve anything it flags) and `brief pin`
(freeze any `@latest`/`@current` refs you wrote to a concrete revision). CI runs the same
check on PRs — that is the backstop; don't bypass it.

**After authoring/publishing a decision — or before opening a PR — run `brief doctor`**
and close what it flags (wire a `// Context:` ref into governed code, pin a floating ref,
publish a draft you now rely on, re-verify a ref the latest revision made stale). It is
advisory, not a gate: it exists so *you* catch latent drift instead of leaving it for a
human to notice later.
