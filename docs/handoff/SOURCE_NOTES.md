# Sources, confidence, and unresolved decisions

**Inspected:** September 22, 2026. These are primary sources. URLs and brief notes are supplied for implementation; they are not a substitute for pinning source revisions and inspecting API contracts. No source repository has been installed or executed as part of this handoff.

Bracketed IDs elsewhere in the packet refer to this file. Uncited architecture and experiment choices are proposed designs, not claims that an upstream tool already implements them.

## S1 — Scientific preprint

**Victor Chang Lee and James M. Rondinelli, _Can Autonomous LLM Agents Execute Multireference Quantum Chemistry Calculations?_, arXiv:2609.13357v1.**

Source: https://arxiv.org/html/2609.13357v1  
Abstract/version entry: https://arxiv.org/abs/2609.13357

The paper specifies 84 molecules and 558 transitions, ORCA 6.1, CASSCF/SC-NEVPT2, aug-cc-pVTZ, RIJCOSX, and AutoAux. Sections II–IV describe knowledge access, diagnostics, the advisory ladder, and evaluation. The baseline, guided, and full-information replication conditions expose different information. State assignments based only on energy ordering are excluded from error statistics. The reference calculations used Molpro, and state-average weighting differences matter. The text mentions an eight-attempt limit; its exact operational accounting needs reconstruction. The code/data availability section still contains a placeholder.

**Use:** Reconstruct and cite the experimental parameters. Do not fabricate the exact dataset selection, complete prompt files, original evaluator, or unpublished expert check.

## S2 — QUESTDB

Source: https://github.com/pfloos/QUESTDB

The repository includes geometries, excitation-energy data, conversion/analysis scripts, and subset-generation tools. It is updated over time. Its current contents are not automatically the paper's historical method-specific selection.

**Use:** Inspect file formats and licensing, pin a revision, preserve original records, and derive a transparent task manifest. Separate agent and evaluator views. Do not bundle the entire repository into the agent's working directory for restricted-information experiments.

## S3 — Prime Agent repository

Source: https://github.com/PrimeIntellect-ai/prime-agent

Prime documents persistent Python/RLM execution, recursive agents, executable skills, background sessions, and refinement of durable supplemental harness state. Refinement is session-local by default. The README explicitly says worker/kernel lifecycle isolation is not a security sandbox.

**Use:** First agent adapter. Inspect and pin the implementation; freeze implicit global state during comparisons. Do not interpret persistence as portable checkpoints, controlled shared scientific memory, or independently validated learning.

## S4 — Prime RLM programming model

Source: https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/rlm.md

The TypeScript host owns provider calls and agent lifecycle, while a persistent Python kernel provides the model-facing programming surface. Working state survives calls and compaction within that runtime.

**Use:** Identify the provider-observation and domain-gateway instrumentation points. Our semantic checkpoint format must remain independent of the running kernel. Do not invent an unsupported native checkpoint/export hook.

## S5 — Prime skills

Source: https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/skills.md

Prime supports Python-backed capability packages and multiple skill-discovery locations.

**Use:** Implement the chemistry gateway as a minimal explicit skill/adapter where practical. Audit discovery so personal/global/built-in capabilities cannot accidentally contaminate a controlled trial.

## S6 — Jev / TypeSafe System One and state

Sources:  
https://docs.typesafe.ai/concepts/system-one  
https://docs.typesafe.ai/concepts/state

Jev takes textual/structured state and produces typed decisions and probabilities rather than free-form generated explanations. The documented interface currently accepts text, not images. State can include relevant records and examples.

**Use:** A replaceable bounded decision provider. Record exact evidence and questions. Do not assume it can inspect orbital images or write code. Confirm available models, SDK methods, version identifiers, access, and prices at integration time; no advertised performance or price is used as an acceptance criterion here.

## S7 — Jev confidence

Source: https://docs.typesafe.ai/confidence

The documented confidence field is derived from the answer probability distribution. It is not an independent scientific verification of an individual workflow outcome.

**Use:** Test/calibrate routing and abstention against development outcomes. Preserve raw fields and avoid interpreting confidence as guaranteed success.

## S8 — GEPA

Source: https://github.com/gepa-ai/gepa

The project supports feedback-driven optimization, including an `optimize_anything` interface for textual artifacts such as configurations and code. Evaluator feedback is part of the optimization loop.

**Use:** Propose and test changes to declared policy surfaces. Keep scientific grading, test data, resource authority, and promotion outside editable candidates. Inspect the pinned adapter API rather than copy stale signatures.

## S9 — Verifiers

Source: https://github.com/PrimeIntellect-ai/verifiers

Verifiers provides environment/evaluation infrastructure for model evaluation and training.

**Use:** Optional wrapper for the domain environment and future training. The canonical portable record and chemistry grader remain ours.

## S10 — Canonical JSON

Source: https://www.rfc-editor.org/rfc/rfc8785

RFC 8785 defines a JSON Canonicalization Scheme for consistent structured serialization.

**Use:** Prefer an established compatible library plus conformance tests for manifest hashing. Raw scientific files are hashed as original bytes, not converted to canonical JSON. Keep exact decimals in explicit typed representations where appropriate.

## S11 — RO-Crate

Source: https://www.researchobject.org/ro-crate/specification/1.2/

RO-Crate specifies structured metadata for packaging research objects.

**Use:** Potential later interoperability export, not a required runtime or a claim that the first bundle is RO-Crate compliant.

## S12 — Codex repository instructions

Source: https://developers.openai.com/codex/guides/agents-md/

OpenAI documents repository instructions through `AGENTS.md`.

**Use:** Keep the handoff's root instructions short and link to detailed specifications. Preserve and reconcile an existing project's instructions rather than replacing them.

## S13 — SoL-Pi

Sources:  
https://nvlabs.github.io/SoL-Pi/  
https://arxiv.org/abs/2609.20519  
https://github.com/NVlabs/SoL-Pi

MIT-licensed extensions for the Pi coding agent (`@earendil-works/pi-coding-agent@0.85.1`): Action Fusion, ObservationPack, Evidence-Preserving Reducer, Online Context Compact. The paper also describes the auto-research loop and two-gate acceptance that produced them; the loop code is not in the repository. Reported results are on coding benchmarks.

**Use:** Second agent adapter and ablatable treatments; method template for harness optimization. See [SOL_PI_DELTA.md](SOL_PI_DELTA.md).

## Initial assumptions register

Codex should turn this table into a tracked project artifact with owners, evidence, and resolution status.

| Question | Current status | Required behavior |
|---|---|---|
| Exact historical task membership | Not recovered in this handoff. | Build a transparent independent manifest; do not claim exact selection. |
| Historical literature/context snapshot | Not packaged here. | Version our permitted corpus and record the difference. |
| Original prompts and evaluator | Public paper points to a placeholder. | Reconstruct conservatively and preserve unresolved decisions. |
| Operational meaning of an attempt | Needs explicit reconstruction. | Track jobs, configurations, retries, and actual resource use separately. |
| New expert convergence check mentioned by the user | Its implementation is not established. | Leave a named extension/adjudication point; do not invent its semantics. |
| Scientific matching/tolerances | Require evidence and review. | No nearest-energy-only matching or arbitrary universal threshold. |
| Solver availability and redistribution | Operator/environment dependent. | Support user-provided installation; verify permission before distribution. |
| Exact Prime/Jev/GEPA/Verifiers revisions | Source docs inspected; revisions not pinned here. | Resolve and lock during M0/integration. |
| Provider-resolved model version | May not be exposed. | Record observable identifiers and the resulting reproducibility limit. |
| Native Prime session portability | Not assumed. | Implement portable semantic checkpoints; label native capabilities separately. |
| API credentials, paid budget, and infrastructure | Not supplied or authorized. | Offline defaults; explicit live-run gating. |
| Original authors' private communications | Not part of this packet. | Do not export private messages or infer unprovided scientific instructions. |

## Claim boundaries

The source checks establish descriptions and implementation pointers, not successful integration. This packet contains no experimental scores generated by our harness, no real solver fixtures, no saved model calls, and no completed scientific reproduction.
