# onoda

onoda is an open harness for running AI agents on scientific computing tasks
and keeping a complete, portable record of what they did. Every model call,
tool call, file and cost is written to a content-addressed store that can be
exported, imported on another machine, replayed offline, graded by a separate
evaluator, and branched from any checkpoint. The first target is quantum
chemistry with ORCA, following Lee and Rondinelli's multireference agent
(arXiv 2609.13357), with NVIDIA's SoL-Pi harness mechanisms and promotion
gates (arXiv 2609.20519) as the variants and acceptance rule.

## Why

Agent results in science are hard to trust and hard to reproduce: the run
lives inside one framework, costs are estimates, and the grader often sees
the same text as the agent. onoda keeps the experiment independent of the
agent that ran it. The evaluator grades claims against a deterministic parser
of the solver's own output, never against the agent's summary, and its
references never enter the agent's store. Anything unknown is recorded as
unknown or `unresolved`, never as a default.

## How it works

- **Run records.** Immutable, hash-chained journals over a content-addressed
  store; export/import bundles, offline replay that fails closed on any drift,
  and forks with explicit lineage and cost accounting.
- **Agent.** The pinned Pi coding agent (0.85.1), optionally with SoL-Pi's four
  mechanisms at a pinned commit, plus onoda's own RLM extension (a persistent
  Python kernel and recursive child runs). Live runs call a real model through
  OpenRouter inside a bubblewrap sandbox, with every setting and spending cap
  stated explicitly.
- **Cases and grading.** Authentic ORCA 5/6 outputs pinned by sha256, a
  deterministic ORCA parser, and a claim evaluator that marks each reported
  value supported, contradicted, unsupported, missing or unresolved.
- **Ablation.** SoL-Pi's add-one ablation: each mechanism alone and all
  together, against a baseline, judged by the two promotion gates.
- **Optimizer.** A GEPA search that rewrites the harness's prompts, tool docs,
  helpers and limits. A candidate is kept only if no capability metric gets
  worse (zero tolerance, zero new false-success claims) and something improves:
  capability or efficiency. Every trial, reflection call and gate decision is
  recorded with its cost.

## What it has shown so far

In live searches on 2026-09-27 (agent `deepseek/deepseek-v4-flash`, reflection
`deepseek/deepseek-v4-pro`, under $5 in all), the optimizer found a tool-doc
rewrite for the RLM arm that removed every false-success claim on the five
acceptance cases: 54 supported and 0 false-success claims against the
starting harness's 49 and 5. On two TD-DFT outputs it never saw, it went from
55/105 and 101/125 correct claims to 105/105 and 125/125 over five runs each.
The rewrite tells the agent to copy energies and units exactly as printed and
to take each state's multiplicity only from the output's headers.

Since then onoda has run ORCA itself. On 2026-10-03 it re-executed the M2
cases with ORCA 6.1.1 on the owner's Mac
([`experiments/orca-exec/RESULTS.md`](experiments/orca-exec/RESULTS.md)); every
comparison with the acquired logs is `unresolved` by design (no tolerance, A6).
In the QUESTDB slice the agent writes and runs its own SA-CASSCF/SC-NEVPT2 jobs.
Suite 1 (2026-10-07, eight molecules, Baseline S0 against Decision S1, two
repeats, `deepseek/deepseek-v4-flash`, about $2.73) ended with 0
false-success claims in both arms and 0 false accepts in the grader audit. S0
identified 51 of 106 target states, 37 within 0.3 eV; S1 46 of 106, 32 within
0.3 eV. The spread between repeats is as large as the gap between arms, so this
declares no winner ([`experiments/questdb/gate-6.md`](experiments/questdb/gate-6.md)
records the result; [`suite-1.md`](experiments/questdb/suite-1.md) the
pre-registration).

Read all of this as a working loop, not a chemistry result. The M2 cases are
small public regression outputs from cclib and none has had human review; the
QUESTDB ladder is a reconstruction from the paper, not the authors' own; and
every live result so far comes from DeepSeek models. Claude Opus 5.5 and
Sonnet 5.5 can now be run through `--pi-models` (below), but no such result has
landed on main yet.

## Status

Milestones M0 to M4 of the handoff in [`docs/handoff/`](docs/handoff/) are
done ([`SOL_PI_DELTA.md`](docs/handoff/SOL_PI_DELTA.md) overrides conflicting
text there). Past M4: the QUESTDB slice with agent-run ORCA jobs (gates 1 to 6
and suite 1 in [`experiments/questdb/`](experiments/questdb/)), forks of
recorded Pi runs, frozen markdown memory tiers for both QUESTDB arms (A75),
shadow-only System One decision models (A67, A70) and a kypp memory shadow, and
a Terminal-Bench slice ([`experiments/tb4-slice/`](experiments/tb4-slice/)).
Before any claim about chemistry: human review of the cases, more and stronger
models on the QUESTDB suite, and a real advisory ladder. Decisions, including the open
ones, are preserved as ordinary Markdown in
[`docs/decisions/`](docs/decisions/); [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) maps the
old A-row ids to them.

## Quick start

```sh
uv sync
uv run pytest          # all suites are offline; no credentials, network, or solver
scripts/demo.sh        # the full clean-workspace sequence, network disabled
```

The demo executes the fixture, exports it, deletes the original store, imports
into a fresh directory, replays offline, reanalyzes the parse step, forks at
the pre-decision checkpoint under two policies, runs both suffixes, compares
lineage and costs, and grades both branches in a separate evaluator store.

## CLI

`onoda` implements the interface proposed in PORTABLE_RUN_SPEC §14. Every
command prints JSON.

```sh
onoda fixture run --run-id demo --store ./store-a
onoda checkpoint list demo --store ./store-a
onoda export demo --store ./store-a --profile agent-visible --out demo.tar.gz
onoda import demo.tar.gz --store ./store-b
onoda inspect demo --store ./store-b --capabilities
onoda replay demo --store ./store-b --offline
onoda reanalyze demo --step parse --store ./store-b [--transform fixture-parser@2]
onoda fork demo --checkpoint <digest> --declaration fork.yaml --new-run demo-branch --store ./store-b
onoda resume demo-branch --mode fixture --store ./store-b
onoda compare demo demo-branch --store ./store-b --include-cost-lineage
onoda evaluate demo-branch --store ./store-b --evaluator-store ./evaluator
```

The mock scheduler stands in for an external system, so its state lives beside
the store (`<store>.scheduler/`) and is never exported.

M2 scientific smoke cases:

```sh
onoda case list
onoda case ingest orca6-dvb-tddft --run-id td6 --store ./store-a
onoda case evaluate td6 --store ./store-a --evaluator-store ./evaluator --reference-case orca5-dvb-tddft
# Live execution needs an operator-supplied ORCA and every setting stated:
onoda case ingest orca6-water-mp2 --run-id live --store ./store-a --execute \
  --enable-live-execution --orca-binary /opt/orca/orca --work-root ./orca-jobs --max-wall-seconds 600
```

M3 Pi runs (after `scripts/pi-setup.sh`, which installs the pinned Pi and
SoL-Pi once from `pi/package-lock.json`):

```sh
onoda pi doctor                     # installed Pi / SoL-Pi / node match the pins
onoda pi variants                   # S0, S1, S1r, S2a-d, S3
onoda pi run orca6-water-mp2 --variant S0 --run-id s0 --store ./store-a \
  --work-dir ./pi-work --script script.json          # offline; outputs synthetic
onoda pi run orca6-water-mp2 --variant S3 --ladder ladder.md ...  # S1 onward need a ladder
onoda pi evaluate s0 --store ./store-a --evaluator-store ./evaluator
onoda pi replay s0 --store ./store-a --work-dir ./replay   # fails closed on any drift
# A live run needs every setting stated (A35):
onoda pi run ... --live --provider P --model M --credential-env P_API_KEY \
  --max-requests 20 --max-cost-usd 5   # S2b/S3 add --reducer-provider R --reducer-model RM
# Fork a recorded run at a checkpoint: replay the parent's prefix, then continue
# on a script or (--live, with its own caps) on a real model (A36):
onoda pi fork s0 --checkpoint <digest> --new-run s0-fork --store ./store-a \
  --work-dir ./fork --script continuation.json --reason "try a different next step"
```

A fork records the parent, checkpoint and prefix, and is refused if any replayed
tool call shows the child a different result than the parent saw. RLM runs,
imported runs, finalized checkpoints and live continuations of reducer variants
are not forkable yet.

M3b ablation (spec format: `tests/ablation_fixtures/spec.yaml`; the chosen tolerances
are `gates/tolerances.yaml`, zero worsening on every capability metric, A49):

```bash
onoda ablation run spec.yaml --store ./store-a --evaluator-store ./evaluator --work-dir ./abl
onoda ablation show sha256:... --evaluator-store ./evaluator   # the whole report
```

A live ablation (`--live`) takes a spec without scripts
(`experiments/ablation-live/spec.yaml`: the six development cases, three
repeats) and runs every arm in the sandbox (A50), one round (every arm on one
case) at a time. It stops before a round the remaining
`--ablation-max-cost-usd` cannot cover at the per-run cap, so the compared arms
always hold the same runs. The reducer may use the agent's own model:

```bash
export OPENROUTER_API_KEY=...
onoda ablation run experiments/ablation-live/spec.yaml --live \
  --provider openrouter --model deepseek/deepseek-v4-flash --credential-env OPENROUTER_API_KEY \
  --max-requests 30 --max-cost-usd 0.05 --ablation-max-cost-usd 2.00 \
  --reducer-provider openrouter --reducer-model deepseek/deepseek-v4-flash \
  --store ./store-a --evaluator-store ./evaluator --work-dir ./abl
```

M4 RLM and optimizer (the gates file is a `GateSpec`, the chosen one is
`gates/gate-spec.yaml`, A47/A49; a split over unreviewed cases is
exploratory, A48):

```bash
onoda pi run orca6-water-mp2 --variant S1r --ladder ladder.md --candidate cand.json ...  # default: the seed
onoda optimize seed > seed.json          # the seed candidate and its provenance
onoda optimize split --seed 1 --final-count 1 > split.json
onoda optimize run --split split.json --gates gates/gate-spec.yaml --variant S1r --ladder ladder.md \
  --scripts ./scripts --reflection-script replies.json \
  --search-id s1 --store ./store-a --evaluator-store ./evaluator \
  --optimizer-store ./optimizer --work-dir ./opt --max-trials 12 --max-reflections 2
onoda optimize status --search-id s1 --optimizer-store ./optimizer
```

`--scripts` holds one scripted-provider script per acceptance case
(`<case_id>.json`) and `--reflection-script` a JSON list of the reflection
model's replies; both are synthetic.

A live search (`--live`, A51) calls real models through OpenRouter and runs
every trial in the bubblewrap sandbox (A50; Linux with `bwrap` on PATH). Every
setting is explicit, and the host needs egress to `openrouter.ai`:

```bash
export OPENROUTER_API_KEY=...
onoda optimize run --live --split split.json --gates gates/gate-spec.yaml --variant S1r \
  --ladder ladder.md --search-id live1 --store ./store-a --evaluator-store ./evaluator \
  --optimizer-store ./optimizer --work-dir ./opt --max-trials 24 --max-reflections 4 \
  --provider openrouter --model deepseek/deepseek-v4-flash \
  --credential-env OPENROUTER_API_KEY --max-requests 30 --max-cost-usd 0.20 \
  --reflection-provider openrouter --reflection-model deepseek/deepseek-v4-flash \
  --reflection-credential-env OPENROUTER_API_KEY --reflection-max-tokens 4000 \
  --reflection-max-cost-usd 0.05 --search-max-cost-usd 5.00
```

`--repeats N` runs every case N times per evaluation and the gates compare
the pooled totals, which damps run-to-run variance in live runs. The chosen
gate spec also accepts a pure capability gain (A52).

The model names are an example, not a tuned choice. The search stops before
an iteration the remaining `--search-max-cost-usd` cannot cover at the
per-trial and per-call caps; `onoda optimize status` and the output's
`spent_usd` show what was spent. `onoda pi run --live` needs the sandbox too,
and `onoda pi run --sandbox` runs a scripted run inside it.

`onoda optimize compare --candidate cand.json` is the live gate check a
candidate must pass before it is promoted to seed (A54). It stores a
`promotion-check` report and its verdict; promoting means then updating
`seed_candidate.json` and `SEED_DIGEST` in the source. `onoda optimize noise` runs one
candidate twice (A/A) to show what moves with nothing changed. Both reports
carry case-bootstrap intervals and headroom (`optimize/diagnostics.py`); these
are advisory and never change a gate verdict.

QUESTDB slice: the agent writes and runs its own SA-CASSCF/SC-NEVPT2 ORCA
jobs (A57–A62). A VTE case (`cases-dev/questdb/`, dev-only) gives the agent a
QUESTDB geometry and its target states (counts per spin and irrep, no
energies). `orca_submit` sends each input over a Unix socket in the Pi work
dir to a host-side broker that runs ORCA, serves byte-identical jobs from a
shared cache as acquired (`cache_hit`, never an execution) and caps
submissions per run (`--max-executions`, cache hits included). The broker's
journal is committed with the run and ingest refuses a spool that disagrees
with it. The grader checks each claimed energy against the parser's reading
of the cited job and pairs states with QUESTDB's SC-NEVPT2 references by
identity (spin, irrep, character), never by energy order. References stay in
the evaluator store.

```bash
onoda pi run-vte questdb-ketene --variant S0 --run-id k0 --store ./store-a \
  --work-dir /tmp/k0 --script script.json \
  --enable-live-execution --orca-binary /opt/orca/orca --orca-work-root ./orca-jobs \
  --max-wall-seconds 1800 --orca-cache ./orca-cache   # every ORCA setting stated; off by default
onoda pi evaluate-vte k0 --store ./store-a --evaluator-store ./evaluator
onoda pi replay k0 --store ./store-a --work-dir ./replay   # broker in replay mode: runs nothing
onoda vte report k0 k1 --store ./store-a --evaluator-store ./evaluator --arm S0 --arm S1
onoda vte audit k0 k1 --store ./store-a   # grade known-wrong variants of real claims
```

`--work-dir` must be short: the broker socket lives under it and macOS allows
AF_UNIX paths under 104 bytes. A live run takes the same live flags as
`pi run` and runs in the sandbox (Seatbelt on macOS). The report gives per arm
coverage, MAE over identified states and false-success claims, and, for two
arms, the MAE over the targets both identified; it declares no winner.
`experiments/questdb/run.sh` runs Baseline (S0) and Decision (S1, the
reconstructed ladder `experiments/questdb/ladder-from-paper.md`) over the eight
cases with repeats on the owner's Mac (`PARALLEL=N` runs them side by side, one
store each); it refuses to start unless the model, credential, caps and ORCA
execution are each set explicitly. Both arms read the frozen memory tiers in
`experiments/questdb/memory/` (A75). Each broker reply carries the parser's
summary of the job (A74) and advisory ladder checks (A66) that nothing grades,
and the agent can build inputs from chosen parameters with `orca_input`.
`REPORT_REMINDER=1` (`--report-reminder`) gives an agent that stops without a
claim one hidden reminder, recorded in the run's config.

Models missing from Pi's built-in catalog, such as Claude Opus 5.5 and
Sonnet 5.5 on OpenRouter, go in a Pi `models.json` passed to `pi run-vte` with `--pi-models`
(`PI_MODELS=` for `run.sh`); `experiments/questdb/pi-models-claude-5.5.json`
is one, and `pi-models-claude-5.5-anthropic.json` is Opus 5.5 on the
Anthropic API directly (`PROVIDER=anthropic CREDENTIAL_ENV=ANTHROPIC_API_KEY
MODEL=claude-opus-5-5:high`). It is copied into the run's isolated agent dir, and a reused work dir
without it drops any earlier run's copy:

```bash
PI_MODELS=experiments/questdb/pi-models-claude-5.5.json MODEL=anthropic/claude-sonnet-5.5 \
  PROVIDER=openrouter CREDENTIAL_ENV=OPENROUTER_API_KEY ... experiments/questdb/run.sh
```

Shadow studies record what other components would have done, beside the grade
and never shown to the agent: `onoda vte shadow` asks a System One decision
model (Jev or Clef, `onoda.systemone`) which ladder rule each job calls for
(A67); `onoda vte shadow-dataset` labels each job ready or not from the grader,
and `onoda decide tune` hill-climbs the question wording against those labels
with GEPA (A70); `onoda vte memory-shadow` seeds a fresh kypp store from the
memory tiers and earlier runs and scores its recall against the tiers.

## Layout

| Module | Responsibility |
|---|---|
| `canonical` | RFC 8785 canonical JSON via the `rfc8785` library; floats rejected; SHA-256 digests |
| `records` | Versioned frozen schemas: artifact, task, step, invocation, event, checkpoint, run, fork, evaluation; migration registry |
| `store` | Content-addressed objects, immutable records, hash-chained per-run journals, crash recovery, disposable SQLite index |
| `controller` | Phase machine over five checkpoint boundaries; idempotent against the journal; semantic restart |
| `jobs` | Mock external scheduler with idempotent submission and reconciliation |
| `budgets` | Reservations, run caps, experiment quotas, marginal / inherited / full-path / physical cost views |
| `decisions` | Decision requests, mock policies, exact-request offline replay |
| `fixture` | Synthetic programs, versioned parsers, mock policies, wavefunction-bound mappings |
| `forks` | Fork declarations, invalidation, visibility cut, `RunView` |
| `memory` | Lessons with provenance, frozen releases, filtered retrieval |
| `replay` | Offline inspection replay and deterministic reanalysis |
| `portability` | Export/import bundles, visibility profiles, thin and redacted bundles, capability inspection |
| `evaluation` | Protected references and graders in a separate evaluator store |
| `context` | SoL-Pi mechanisms as recorded fixtures: ObservationPack, compaction, reducer, action fusion |
| `adapters.agents` | The `AgentAdapter` seam for Pi (and Prime), with a deterministic mock |
| `science.contract` | M2 observation schema (exact decimal text, units, line evidence, explicit problems) and sha256-pinned case manifests |
| `science.orca_parser` | Deterministic ORCA 5/6 output parser (`orca-parser@2`) |
| `science.orca_local` | Local ORCA scheduler for an operator-supplied binary; disabled by default, idempotent by key |
| `science.evaluator` | External evaluator skeleton: execution, parse integrity, state identification, reference comparison under a declared policy |
| `adapters.pi.contract` | M3 contract: runtime pins, spool wire format, scripted-provider script, task bundle, claim, variants S0–S3 and their `sol-pi.json`, gateway policy |
| `adapters.pi.runtime` | `doctor`, launch builder (isolated agent dir, extension load order, live-setting gate), process runner |
| `adapters.pi.ingest` | Spool → run record: decision/tool/acquisition receipts, fused-call decomposition, ObservationPack handles as store digests, compactions, checkpoints |
| `adapters.pi.task` | Task bundle staging and claim grading against the deterministic parser, in the evaluator store |
| `adapters.pi.run` | One run end to end (case or VTE task) and replay under recorded responses |
| `adapters.pi.fork` | Fork a recorded Pi run at a checkpoint: replay the prefix, then continue on a script or a live model (A36) |
| `adapters.pi.sandbox` | The bubblewrap (Linux) and Seatbelt (macOS) sandbox for Pi runs (A50) |
| `adapters.pi.vte_contract` | VTE task bundle and claim; re-exports the broker wire protocol |
| `adapters.pi.vte` | VTE bundle staging, identity grading of a run against the deterministic parser (in the evaluator store) and the per-arm report |
| `adapters.pi.vte_audit` | Mutation audit of the VTE grader: grade known-wrong variants of real claims and count what it still credits |
| `adapters.pi.vte_shadow` | Shadow decisions per broker job from a System One model, and the labelled dataset for `decide tune` (A67, A70) |
| `adapters.pi.vte_memory_shadow` | kypp memory shadow scored against the markdown memory tiers |
| `adapters.tb` | Terminal-Bench slice: Harbor agent, spend proxy, paired runs per arm |
| `science.orca_broker` | Host-side ORCA broker for `orca_submit`: wire protocol, cache, attempt cap, journal, replay mode |
| `science.orca_sandbox` | Sandbox for agent-written ORCA inputs (A65) |
| `science.orca_summary` | The parser's per-job summary sent with every broker reply (A74) |
| `science.orca_checks` | Deterministic, advisory ladder checks on each job's output (A66) |
| `science.vte` | VTE case manifests and evaluator-only reference rows |
| `pi/extensions/` | onoda's Pi extensions (TypeScript): task tools, scripted provider, recorder writing the fsynced spool |
| `ablation.contract` | M3b contract: arms, spec, outcome vector, metric names and directions, tolerances, gate result |
| `ablation.metrics` | One run's outcome vector from the agent store and its claim evaluation; the A15 reducer-loss probe |
| `ablation.gates` | Two-gate comparison, non-dominated set, S3-vs-sum-of-S2 interaction (pure, `Decimal`) |
| `ablation.run` | Runs every arm on every case and writes the report to the evaluator store |
| `optimize` | GEPA search, gates, reflection, `compare` (promotion, A54) and diagnostics (noise, intervals, headroom) |
| `systemone` | Client for the System One decision models Jev and Clef (A67) |
| `decision_tune` | GEPA over a decision model's question wording against labelled examples (A70) |
| `science.runner` | Records a case as a run (acquired or executed), parses it, and evaluates it in a separate store |

Start with the [feature map](docs/features/README.md) for user journeys, entry points,
functional tests and commands to drive each area.

## Capability matrix

What was actually demonstrated, and on what. "Fixture" means the synthetic
workflow in `onoda.fixture`; none of these rows is a scientific result.

| Capability (PORTABLE_RUN_SPEC §1) | Status | Evidence |
|---|---|---|
| Inspect / replay | Demonstrated on fixture, offline, after import into a fresh store | T01, T09; `tests/test_replay.py`, `tests/test_end_to_end.py` |
| Reanalyze | Demonstrated for the pure parse step; new derivation receipts, originals untouched | T02; `tests/test_replay.py` |
| Reexecute | Fixture programs via the mock scheduler; the M2 cases with real ORCA 6.1.1 on the owner's Mac (differences reported, verdicts `unresolved`) | `inspect --capabilities`, `tests/test_orca_local.py`, `experiments/orca-exec/RESULTS.md` |
| Parse authentic solver output | ORCA 5.0/6.0 TD-DFT, EOM-CCSD, ADC(2), STEOM-CCSD, MP2 outputs; values cross-checked against cclib | `tests/test_orca_parser.py` |
| Evaluate against a reference | ORCA 6.0 vs 5.0 TD-DFT: exact differences reported, verdict `unresolved` (no justified tolerance, A6) | `tests/test_smoke_cases.py`, `scripts/demo.sh` |
| Resume | Semantic restart from checkpoints; native session resume refused | T10, T14; `tests/test_jobs.py`, `tests/test_controller.py` |
| Fork | Quiescent checkpoints; policy, parser, input, calc-config and memory interventions with invalidation | T03, T05, T07; `tests/test_forks.py` |
| Fork a Pi run | Replays the parent's prefix and continues on a script or a live model; refused if a replayed tool call returns a different result (a scripted parent's prefix also fails on any context drift). Not for RLM or imported runs | `tests/test_pi_fork.py` |
| Agent runs (Pi) | Real pinned Pi 0.85.1 and SoL-Pi run offline under a scripted provider, for S0, S1, S2a, S2b, S2d and S3 (S2c's mechanism is loaded inside S3 but never triggered); spooled, ingested, verified by offline replay, claims graded. Live runs through OpenRouter (DeepSeek V4 Flash) in ablations, searches and the QUESTDB suite | `tests/test_pi_extensions.py`, `tests/test_pi_end_to_end.py` |
| Replay an agent run | Re-runs Pi under the recorded responses and fails closed on any change to what the model was shown | `tests/test_pi_end_to_end.py` |
| Add-one ablation (M3b) | S1, S2a–d and S3 on a case under one scripted script: outcome vectors, activation per mechanism and model, reducer-loss probe, two-gate comparison against S1, S3 interaction. Measured payload bytes fall under ObservationPack; capability gates stay `unresolved` because the fixture tolerances leave `count.unresolved` undeclared (the chosen ones, A49, are in `gates/`). Synthetic, not a mechanism verdict (A40) | `tests/test_ablation_*.py`, `scripts/demo.sh` |
| RLM extension (S1r) | Real Pi with `ipython` cells, committed JSON state, a child Pi run recorded as its own run and charged to the parent, replay of the whole tree; scripted provider only | `tests/test_pi_rlm_extension.py`, `tests/test_rlm_end_to_end.py` |
| Harness optimization (M4) | GEPA proposes candidate components from agent traces; SoL-Pi's gates accept or reject each on acceptance cases only. A scripted two-proposal search on real Pi rejects one (a false success) and accepts one (fewer context bytes). Synthetic, not an improvement (A32) | `tests/test_optimize*.py`, `scripts/demo.sh` |
| Live search (A51) | Ran live on 2026-09-27 (above); offline tests use a fake transport for reflection receipts and the search-wide spend cap | `tests/test_live_optimizer.py` |
| Sandbox (A50) | Pi runs inside bubblewrap (Seatbelt on macOS): `bash` and the RLM kernel have no network, no credential and no host files; file tools stay in the workspace and bundle | `tests/test_pi_sandbox.py` |
| Agent-run ORCA jobs (VTE, A57–A59) | Scripted Pi on real Pi submits jobs through the host broker to a labelled stand-in executable (one execution, one cache hit), claims a state, is graded by identity and replays offline with zero executions (the replay broker serves the recorded outputs and repeats the recorded cache-hit flag, never `executed_here`). Live: agents wrote and ran real ORCA 6.1.1 jobs in gates 1 to 6 and suite 1 | `tests/test_pi_vte_end_to_end.py`, `experiments/questdb/` |
| Repeat experiment | Not implemented | — |

## Acceptance tests

| Test | Where |
|---|---|
| T01 clean-workspace round trip | `test_end_to_end.py`, `test_portability.py`, `scripts/demo.sh` |
| T02 intermediate reanalysis | `test_replay.py` |
| T03 fork before a decision | `test_forks.py::test_t03_*` |
| T04 changed-request replay rejection | `test_decisions.py` |
| T05 no inherited future | `test_forks.py::test_t05_*` |
| T06 same spec, distinct invocations | `test_decisions.py` |
| T07 dependency invalidation | `test_forks.py::test_t07_*` |
| T08 corruption and missing closure | `test_portability.py` |
| T09 no host/vendor dependency | `test_portability.py`, `test_replay.py`, `test_end_to_end.py` |
| T10 crash-safe submission | `test_jobs.py` |
| T11 ledger and fork budgets | `test_budgets.py`, `test_forks.py::test_t11_*` |
| T12 safe import | `test_portability.py` |
| T13 secret and evaluator isolation | `test_portability.py`, `test_evaluation.py` |
| T14 checkpoint capability honesty | `test_controller.py` |
| T15 atomicity, ordering, versioning | `test_core.py` |
| T16 evaluation is a separate derivation | `test_evaluation.py` |
| SoL-Pi M1 fixtures (handles, compaction, reducer, fusion) | `test_context.py` |
| T17 authentic fixed-input case (partial: acquired outputs, not executed here) | `test_orca_parser.py`, `test_smoke_cases.py` |
| T18 honest partial outputs | `test_orca_parser.py` (truncation, missing diagnostics, malformed numbers, wrong units, conflicting listings), `test_smoke_cases.py` |
| T19 controlled baseline (partial: Pi instead of Prime; isolated agent dir, no global extensions/skills/context files, pinned context, gateway budget; sandboxed when live or with `--sandbox`) | `test_pi_runtime.py`, `test_pi_extensions.py`, `test_pi_end_to_end.py` |

T17 is only partly met: ORCA has now executed the fixed inputs, but none has
had a human case review (assumptions A7, A26). T19 is partial (above). T20 needs semantic restart of a
Pi run, which M3 records the state for but does not implement (A36; forks are); T21–T25
belong to later milestones.

## Limitations

- Fixture costs are simulated and priced from a fixture table; they are not
  spend. Unknown costs are reported as unknown, never zero.
- One authoritative writer per run; no locking for concurrent writers.
- Forks require quiescent checkpoints. In-flight forks are not supported.
- Live results so far use DeepSeek models (`deepseek/deepseek-v4-flash`, with
  `deepseek/deepseek-v4-pro` for reflection) on unreviewed cases; scripts,
  claims and ladders in the tests are synthetic.
- The advisory ladder and allowed documentation are unresolved (A33): S1
  onward, the RLM arm S1r included, refuse to run without an operator-supplied
  ladder. The QUESTDB ladder is a reconstruction from the paper (A61).
- Pi runs are sandboxed only when asked (`--sandbox`) or live (A50), with
  bubblewrap on Linux or Seatbelt on macOS. Scripted runs and replay are unsandboxed by
  default. Inside the sandbox the Pi process itself keeps network access to
  reach its provider, and the file-tool path check has a time-of-check gap.
- The spool's HMAC chain is tamper evidence against the agent's own tools
  rewriting the spool casually, not a sandbox: a process that can read the Pi
  process's memory can forge it, and dropping trailing lines only makes the
  run incomplete.
- `onoda pi replay` executes the recorded tool calls, so it refuses runs that
  arrived through import; replay only runs you recorded yourself.
- Resume of Pi runs is not implemented (A36). Forks are, except for RLM runs,
  imported runs, finalized checkpoints and live continuations of reducer
  variants. A real (live) provider's reducer and compaction-summary calls are
  not spooled (see `docs/ADAPTER_SEAM.md` §2).
- Scientific tolerances, task membership, and the expert convergence check are
  unresolved; see [`docs/decisions/`](docs/decisions/). Upstream
  revisions are pinned in [`provenance/sources.json`](provenance/sources.json).
- `RunView` is a snapshot taken at construction and must be rebuilt after new
  events.
- The M2 cases are cclib's small STO-3G regression inputs, not the paper's
  task set (A1), and none has been reviewed by a human yet (A26). Their
  redistribution rests on cclib's BSD-3 licence (A25).
- EOM-CCSD and ADC(2) outputs print no multiplicity on their root lines, so
  state identification is `unresolved` for those cases (A28).
- Case runs support inspect, export/import, and offline replay. Fixture-only
  commands (`resume`, `fork`, `reanalyze`, `compare`, `evaluate`) refuse them,
  and `inspect --capabilities` reports those as unsupported by the record. An
  interrupted case run is continued by rerunning `onoda case ingest` with the
  same run id.
- `case evaluate` re-checks an acquired run against the pinned case (manifest
  digest, log sha256, a fresh parse). A live run's log has no pinned digest,
  so its execution receipt and stand-in flag are trusted as recorded; do not
  grade live runs from imported bundles you did not produce.
- A live run's timeout kills the ORCA process group. Real ORCA has run only on
  the owner's Mac (ORCA 6.1.1, arm64); CI and the tests use a labelled stand-in.

- An ablation report over scripted runs is synthetic (A40): every arm replays
  the same turns, so capability is identical by construction, and tokens and
  cost are the script's. Its `context_bytes` are measured payload sizes, a
  traffic proxy, not tokens. S2c's compaction does not trigger on runs this
  short. The fixture spec's capability gates stay `unresolved`; `gates/tolerances.yaml` (A49) resolves them.
  The reducer-loss probe (A15) has only seen scripted receipts.
- Ablations do not resume: one that stops partway keeps its runs, and a rerun
  needs a new `ablation_id` (A41). An RLM ablation (`rlm: true`) runs every arm
  on S1r (A55).
- RLM kernel state is recorded only as JSON committed with `commit_state`
  (A43); anything left only in kernel memory is lost with the process, and
  restarting from committed state is not implemented (A36). The kernel runs
  in the same inner sandbox layer as Pi's `bash` when the run is sandboxed
  (A50).
- Optimizer searches over the unreviewed cases are exploratory (A48). Its
  gates are `gates/gate-spec.yaml` (zero capability tolerances, A47/A49;
  capability gains accepted, A52), and reflection sees verdict words and
  reason classes, never references (A53). Live runs of the same case vary, so
  use `--repeats`. GEPA's own state file is scratch, not the record (A46).

## Next

Human review of the M2 cases; the QUESTDB suite on stronger models (Claude
Opus 5.5 and Sonnet 5.5 through `--pi-models`); a real advisory ladder (A33);
held-out cases beyond TD-DFT; semantic restart of Pi runs from committed state
(A36); justified scientific tolerances (A6, A18).
