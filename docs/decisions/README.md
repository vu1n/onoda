# Decision context

These ordinary Markdown documents preserve the scientific assumptions, reasons, known
gaps and A-row history from the former Brief documents. Status labels are preserved
as historical metadata: a draft is still a draft. This move does not approve a
proposal, change a threshold, or alter the implementation. Find current behavior,
entry points and functional tests in the [feature map](../features/README.md).

The [optimizer validation-slice proposal](../proposals/optimizer-validation-slice.md)
remains **unratified**. Search trials still use acceptance cases only.

## Legacy identifiers

Stored run records retain their `doc://` identifiers. For
`doc://onoda/<id>@<revision>#<anchor>`, read `<id>/versions/v<revision>.md`
below (numeric revisions are four digits); without a revision or for `draft`, read
`<id>.md`. Anchors are preserved as HTML ids. In particular, the persisted
`doc://onoda/vte-energy-tolerance#identified-within-tolerance` value still identifies
[vte-energy-tolerance.md](vte-energy-tolerance.md#identified-within-tolerance).
No CLI or resolver is needed, and serialized records are unchanged.

The [assumptions register](../ASSUMPTIONS.md) retains A-row lookups. Earlier
versions and [historical approvals](history/README.md) retain their original context.

## Documents

| Document | Status at migration |
|---|---|
| [Agent commands run sandboxed: bwrap two layers on Linux, one Seatbelt layer on macOS, refused elsewhere](agent-sandbox.md) | active |
| [An opt-in fault plan makes the broker serve a known failure in place of one job's output, recorded as injected, never credited, and measured against clean controls](broker-fault-injection.md) | active |
| [Case bytes are pinned by sha256, recorded as acquired, and third-party dev data stays under cases-dev/](case-data-provenance.md) | active |
| [Decision models (Jev, Clef) and ladder checks are advice only; they never grade](decision-models-shadow-only.md) | active |
| [Decision-model wording is tuned against grader labels, offline, and only the wording changes](decision-prompt-tuning.md) | draft |
| [No DSPy dependency; single-author packages are allowed](dependency-policy.md) | draft |
| [Evaluator references and credentials never reach the agent store or an agent-visible export](evaluator-isolation.md) | active |
| [Every live setting defaults off, needs explicit values, and fails closed](live-off-by-default.md) | active |
| [States are matched by identity, never by energy order; a missing tolerance is unresolved, not pass](match-by-identity.md) | active |
| [QUESTDB agents carry frozen markdown memory tiers between runs; kypp runs only in shadow](memory-tiers.md) | draft |
| [The optimizer changes only allowlisted components, sees verdict classes only, and the seed moves only by promotion](optimizer-scope.md) | active |
| [Agent ORCA jobs run only in the host broker, each in its own job sandbox, under one execution cap](orca-broker-boundary.md) | active |
| [The deterministic ORCA parser is the only source of graded values; ORCA's exit code is not trusted](orca-parser-authoritative.md) | active |
| [An orca_submit reply shows the parser's summary of the output, not the whole output](orca-reply-summary.md) | draft |
| [Pi is the first agent adapter, pinned at 0.85.1 with SoL-Pi at 1559b5c](pi-first-pinned-stack.md) | active |
| [Gates allow zero capability worsening and zero false-success increase; a missing tolerance is unresolved](promotion-gates.md) | active |
| [Run records are immutable, single-writer, float-free, and restart only from semantic checkpoints](record-integrity.md) | active |
| [Offline replay fails closed on any request change; scripted runs are labelled synthetic](replay-fails-closed.md) | active |
| [An identified VTE state is accurate only within 0.3 eV of its SC-NEVPT2 reference](vte-energy-tolerance.md) | draft |
| [A VTE claim counts only if the one cited job printed it, with its irrep, in that job's results table](vte-grading-contract.md) | active |
| [vte report shows per-arm process metrics: jobs per accurate state, failed-job share, cross-run stability](vte-report-process-metrics.md) | active |
| [What a VTE agent is given: geometry, point group and target counts; no energies, no ORCA manual](vte-task-inputs.md) | active |
