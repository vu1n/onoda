# Portable Scientific-Agent Harness — Codex Handoff

**Version:** 0.1 • **Prepared:** September 22, 2026  
**Status:** Build specification, not an implemented harness or a completed reproduction.

## The assignment

Build a portable, inspectable environment for reproducing scientific-agent experiments and comparing harness designs. Start with the multireference quantum-chemistry experiment discussed in the source notes. Use Prime Agent as the first research-agent adapter. Add Jev, shared experience memory, and GEPA as independently measurable extensions. Preserve a path to training an open-weight worker later.

**The central requirement is that an experiment belongs to its data and provenance, not to the agent framework that executed it.** Someone should be able to export a run, import it on another machine, inspect its evidence, reproduce supported intermediate computations, and branch from an explicit checkpoint without losing lineage.

Do not promise that replaying a transcript reproduces a calculation, or that making the same model request produces the same answer.

## Start here

Give Codex [`CODEX_KICKOFF.md`](CODEX_KICKOFF.md), with this folder available in its workspace. The kickoff asks it to implement the first two milestones, not merely write another plan. All `labrun` commands in this packet are **proposed interfaces to implement**, not commands available today.

| File | Purpose |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Short implementation guardrails. Merge with existing repository instructions; do not overwrite them blindly. |
| [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md) | Architecture, integration boundaries, implementation order, and delivery gates. |
| [`docs/PORTABLE_RUN_SPEC.md`](docs/PORTABLE_RUN_SPEC.md) | Authoritative data, checkpoint, replay, fork, export, and cost-accounting requirements. |
| [`docs/EXPERIMENT_PLAN.md`](docs/EXPERIMENT_PLAN.md) | Scientific reconstruction, information controls, ablations, and evaluation. |
| [`docs/ACCEPTANCE_TESTS.md`](docs/ACCEPTANCE_TESTS.md) | Observable acceptance tests, including clean-machine portability and counterfactual branches. |
| [`docs/SOL_PI_DELTA.md`](docs/SOL_PI_DELTA.md) | Required changes to account for SoL-Pi (Pi adapter, context mechanisms, two-gate promotion). Overrides conflicting text elsewhere. |
| [`docs/SOURCE_NOTES.md`](docs/SOURCE_NOTES.md) | Primary sources, verified capabilities, and unresolved reconstruction details. |
| [`examples/experiment.template.yaml`](examples/experiment.template.yaml) | Configuration design seed; deliberately disabled for live execution. |
| [`examples/fork.template.yaml`](examples/fork.template.yaml) | Explicit branch-change declaration and provenance requirements. |

## First definition of done

An offline, synthetic example can be executed, exported, imported into a clean directory with no original session or database, replayed without network access, reanalyzed from raw artifacts, and forked at a safe checkpoint. Changing a decision invalidates dependent outputs. The original remains unchanged, and both branches retain correct evidence and cost lineage.

This establishes the infrastructure contract. It does **not** establish scientific correctness. A separate fixed-input scientific smoke test must pass before autonomous chemistry results are reported.

## Scope discipline

No paid calls, large benchmark runs, external publishing, solver redistribution, model training, or deployment is authorized by this packet. Implement mocks and interfaces first. Enable live execution only with explicit operator configuration, credentials, and budgets. Source URLs were inspected during preparation; mutable repositories still need revision pinning during milestone M0.
