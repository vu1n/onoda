# Assumptions register (moved)

The decisions this register tracked now live in [`.brief/docs/`](../.brief/docs/) as governed
decision docs (see the brief section of `CLAUDE.md`). Each row A1–A73 was verified against the
code on 2026-10-04 and carried, verbatim, into the decision below; drift found on the way is
listed in that decision. Record new assumptions and decisions there, not here. Existing
`A<n>` citations in code and docs keep their meaning: look the id up in this table.

| Row | Question | Decision |
|---|---|---|
| A1 | Exact historical task membership (which molecules/transitions the paper actually used) | [`case-data-provenance`](../.brief/docs/case-data-provenance.md) |
| A2 | Historical literature/context snapshot available to the original agents | [`vte-task-inputs`](../.brief/docs/vte-task-inputs.md) |
| A3 | Original prompts and evaluator | [`evaluator-isolation`](../.brief/docs/evaluator-isolation.md) |
| A4 | Operational meaning of "an attempt" (the paper's eight-attempt limit) | [`orca-broker-boundary`](../.brief/docs/orca-broker-boundary.md) |
| A5 | Semantics of the "new expert convergence check" mentioned by the user | [`decision-models-shadow-only`](../.brief/docs/decision-models-shadow-only.md) |
| A6 | Scientific state-matching / reproduction tolerances | [`match-by-identity`](../.brief/docs/match-by-identity.md) |
| A7 | Solver (ORCA) availability and redistribution rights | [`live-off-by-default`](../.brief/docs/live-off-by-default.md) |
| A8 | Exact Prime / Jev / GEPA / Verifiers revisions | [`pi-first-pinned-stack`](../.brief/docs/pi-first-pinned-stack.md) |
| A9 | Provider-resolved model version (e.g. which underlying checkpoint served a request) | [`live-off-by-default`](../.brief/docs/live-off-by-default.md) |
| A10 | Native Prime/Pi session portability | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A11 | API credentials, paid budget, and infrastructure authorization | [`live-off-by-default`](../.brief/docs/live-off-by-default.md) |
| A12 | Original authors' private communications | [`evaluator-isolation`](../.brief/docs/evaluator-isolation.md) |
| A13 | Pi / SoL-Pi revisions and extension API stability | [`pi-first-pinned-stack`](../.brief/docs/pi-first-pinned-stack.md) |
| A14 | Whether ObservationPack thresholds (10 KiB archive, 2-request resend window) suit ORCA log sizes | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A15 | Reducer completeness on scientific logs (does a compact receipt drop a convergence warning or root flip?) | [`orca-parser-authoritative`](../.brief/docs/orca-parser-authoritative.md) |
| A16 | Mechanism transfer across models (paper reports lower activation on Opus 5 than the tuning model) | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A17 | Prime's base Pi version and its compatibility with the SoL-Pi extension API | [`pi-first-pinned-stack`](../.brief/docs/pi-first-pinned-stack.md) |
| A18 | Capability-gate tolerances for the two-gate promotion rule | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A19 | Auto-research loop code (the search procedure that produced SoL-Pi's mechanisms) | [`pi-first-pinned-stack`](../.brief/docs/pi-first-pinned-stack.md) |
| A20 | How are compute/solver costs represented before a real solver is integrated? | [`live-off-by-default`](../.brief/docs/live-off-by-default.md) |
| A21 | Can canonical records carry floating-point numbers? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A22 | Can more than one process append to a run's journal? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A23 | Can a fork be taken from any point in a running trajectory? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A24 | Does resuming a run replay the original process/kernel state? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A25 | Where do authentic ORCA outputs come from without an ORCA licence here? | [`case-data-provenance`](../.brief/docs/case-data-provenance.md) |
| A26 | Who reviews the fixed-input cases? | [`case-data-provenance`](../.brief/docs/case-data-provenance.md) |
| A27 | Units of `FINAL SINGLE POINT ENERGY` | [`orca-parser-authoritative`](../.brief/docs/orca-parser-authoritative.md) |
| A28 | Multiplicity of EOM-CCSD / ADC(2) roots | [`orca-parser-authoritative`](../.brief/docs/orca-parser-authoritative.md) |
| A29 | Which cclib validates the parser? | [`orca-parser-authoritative`](../.brief/docs/orca-parser-authoritative.md) |
| A30 | Reexecution tolerance between ORCA versions | [`match-by-identity`](../.brief/docs/match-by-identity.md) |
| A31 | Which mechanism each S2 letter names | [`pi-first-pinned-stack`](../.brief/docs/pi-first-pinned-stack.md) |
| A32 | How are Pi runs exercised without model credentials? | [`replay-fails-closed`](../.brief/docs/replay-fails-closed.md) |
| A33 | Content of the advisory ladder and the allowed documentation | [`vte-task-inputs`](../.brief/docs/vte-task-inputs.md) |
| A34 | What may differ between a recorded Pi request and its replay? | [`replay-fails-closed`](../.brief/docs/replay-fails-closed.md) |
| A35 | Live Pi runs (a real provider) | [`live-off-by-default`](../.brief/docs/live-off-by-default.md) |
| A36 | Fork and resume of Pi runs | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A37 | When do Pi tool outputs enter the store? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A38 | What bounds a Pi tool call? | [`agent-sandbox`](../.brief/docs/agent-sandbox.md) |
| A39 | Which capability tolerances does M3b use? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A40 | What does an ablation over scripted runs show? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A41 | How are an arm's cases combined, and what counts as an efficiency improvement? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A42 | Which cases may an ablation read? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A43 | Can a checkpoint depend on RLM kernel memory? | [`record-integrity`](../.brief/docs/record-integrity.md) |
| A44 | Who pays for an `rlm()` child's requests? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A45 | What may the optimizer change? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A46 | Is GEPA's run-dir state part of the record? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A47 | Capability-gate tolerances for the optimizer (instance of A18) | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A48 | Can the six pending-review cases carry an acceptance/final split? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A49 | Which capability tolerances do the ablation and optimizer gates use? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A50 | How are Pi runs sandboxed? | [`agent-sandbox`](../.brief/docs/agent-sandbox.md) |
| A51 | How does a live optimizer search run and what may it spend? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A52 | Must an accepted optimizer candidate improve efficiency? | [`promotion-gates`](../.brief/docs/promotion-gates.md) |
| A53 | What may reflection learn from the evaluator? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A54 | How does a candidate become the seed? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A55 | How does an ablation run on the RLM harness? | [`optimizer-scope`](../.brief/docs/optimizer-scope.md) |
| A56 | `<S**2>` on MDCI `IROOT` lines | [`orca-parser-authoritative`](../.brief/docs/orca-parser-authoritative.md) |
| A57 | What does a VTE task give the agent? | [`vte-task-inputs`](../.brief/docs/vte-task-inputs.md) |
| A58 | How is a reported VTE graded? | [`vte-grading-contract`](../.brief/docs/vte-grading-contract.md) |
| A59 | How do agent-submitted ORCA jobs run, and what counts as an execution? | [`orca-broker-boundary`](../.brief/docs/orca-broker-boundary.md) |
| A60 | How is a Pi run sandboxed on macOS? | [`agent-sandbox`](../.brief/docs/agent-sandbox.md) |
| A61 | Which advisory ladder does the Decision arm get? | [`vte-task-inputs`](../.brief/docs/vte-task-inputs.md) |
| A62 | Which QUESTDB molecules and rows? | [`case-data-provenance`](../.brief/docs/case-data-provenance.md) |
| A63 | How does `onoda vte report` pool runs and compare two arms? | [`vte-grading-contract`](../.brief/docs/vte-grading-contract.md) |
| A64 | Does the claimed irrep mean the same thing in ORCA's output and in QUESTDB? | [`vte-grading-contract`](../.brief/docs/vte-grading-contract.md) |
| A65 | How are agent-submitted ORCA jobs confined? | [`orca-broker-boundary`](../.brief/docs/orca-broker-boundary.md) |
| A66 | Which ladder checks does the host run on an agent's ORCA output? | [`decision-models-shadow-only`](../.brief/docs/decision-models-shadow-only.md) |
| A67 | How do System One decision models (Jev, Clef) enter the QUESTDB research? | [`decision-models-shadow-only`](../.brief/docs/decision-models-shadow-only.md) |
| A68 | What did the first live shadow pass change? | [`decision-models-shadow-only`](../.brief/docs/decision-models-shadow-only.md) |
| A69 | How does a Pi run on a benchmark task that brings its own container (Terminal-Bench via Harbor)? | [`agent-sandbox`](../.brief/docs/agent-sandbox.md) |
| A70 | How is a decision model's question wording tuned? | [`decision-prompt-tuning`](../.brief/docs/decision-prompt-tuning.md) (draft) |
| A71 | Does a supported claim from a job without printed irreps identify a target? | [`vte-grading-contract`](../.brief/docs/vte-grading-contract.md) |
| A72 | How long may one agent `bash` command run, and how does the agent get back to an earlier job's output? | [`agent-sandbox`](../.brief/docs/agent-sandbox.md) |
| A73 | What are the agents told about how a VTE claim is graded? | [`vte-grading-contract`](../.brief/docs/vte-grading-contract.md) |
| A74 | What does an `orca_submit` reply show, and how does the agent find its earlier jobs? | [`orca-reply-summary`](../.brief/docs/orca-reply-summary.md) (draft) |
| A75 | What memory do QUESTDB agents carry between runs? | [`memory-tiers`](../.brief/docs/memory-tiers.md) (draft) |

