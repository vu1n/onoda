# Decision models (Jev, Clef) and ladder checks are advice only; they never grade

**Status at migration:** active. This migration does not change that status.


<a id="never-grade"></a>
## Keep decision models and advisory checks out of grading

System One decision models run in shadow mode: they see agent-visible data only (no reference values), the agent never sees their answers, and their answers are stored as `vte-shadow-<provider>` evaluations that the grader and gates never read. Host ladder checks (`orca_checks`) attach printed-number advice to every broker reply for both arms and are never graded. The parser, evaluator and gates stay the only judges.

**Why.** The point of the levers is to measure whether a decision model agrees with the grader; if its answer could reach the grade or the agent, there would be nothing left to measure.

### Invariant
- The VTE grader selects only `{run}.vte-claims-v*` evaluations; only `shadow_report` reads shadows.
- Live decision calls need `--enable-live-decisions`; credentials come from the environment and are never recorded.
- Shadow state excerpts are capped at 12,000 characters and carry no reference value.
- `job_checks` compares printed numbers only and is applied to both arms.
- A5's expert convergence check is not built; nothing invents its criteria.

### Known gaps (not yet enforced; owner to decide)
- No test asserts that adding a shadow evaluation leaves a grade unchanged; the separation is by evaluation id prefix.
- A70 (the decision-prompt hill climber) lives on open PR #21 and is not on main yet.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

#### A5: Semantics of the "new expert convergence check" mentioned by the user

Register status: unresolved. Owner milestone: M4.

Leave a named, unresolved adjudication extension point (BUILD_SPEC §4, Jev extension); do not invent its criteria. A textual decision provider is not a visual expert.

#### A66: Which ladder checks does the host run on an agent's ORCA output?

Register status: decided (default, job checks). Owner milestone: job checks.

Three that need only printed numbers, from `onoda.science.orca_checks`, attached to every broker reply and journaled with it. (1) Criterion 1: the lowest SA-CASSCF root (`LOWEST ROOT`, or the final CASSCF energy of a single-root job), only when CASSCF converged, against the converged Hartree-Fock total energy of this job or an earlier job of the run whose basis line and Cartesian coordinates ORCA printed identically (the latest such job); none found is said, not guessed. Approximations such as RIJCOSX are not part of that key. (2) Criterion 3: active orbitals counted per irrep, as a summary only, because the space the agent intended is not in its input. (3) State averaging: ORCA's printed per-root weights must all be equal, the convention of QUESTDB's Molpro references (arXiv 2609.13357: mismatched weights were the paper's largest replication error). The checks are advice for the agent: never observations, never read by the grader, no reference values. Both arms of the ladder experiment get them, so the S0-vs-S1 comparison still isolates the ladder text. Judgement calls (rules D, E, G, criterion 2) are left to the agent; a decision model for those would be a separate, shadow-mode step.

#### A67: How do System One decision models (Jev, Clef) enter the QUESTDB research?

Register status: decided (default, shadow levers). Owner milestone: shadow levers.

As research levers in shadow mode first: `onoda vte shadow <run> --provider jev\|clef --enable-live-decisions` shows a decision model every broker job of a finished run and asks two typed questions, which ladder rule (A–H) applies first and whether the job is ready to report. The rule options are the rule paragraphs of the ladder file (default: the A61 reconstruction, the same for both arms), so a GEPA-tuned ladder and the model never drift apart. The state is agent-visible data only (targets, the job's input echo and verbatim CASSCF/NEVPT2 excerpts capped at 12,000 characters, parser status, A66 checks); no reference value. The agent never sees an answer. Answers, with the exact request and response bytes and the provider's reported cost (unknown stays unknown), are one `Evaluation` (`vte-shadow-<provider>`) in the evaluator store, and `onoda vte report` presents them beside the grader's outcome per job (identified, unidentified, rejected, uncited). They never grade: the parser, evaluator and gates stay the only judges (Vu, 2026-09-29). Jev runs through OpenRouter's Decisions API (`typesafe/jev-1.13`); Clef through Workers AI (`@cf/cloudflare/clef-flash`), whose envelope is unwrapped. Live calls default off and need the provider's credentials in the environment, never recorded. An advisory arm (the agent may ask the model) is a later step, only if the shadow data justify it.

#### A68: What did the first live shadow pass change?

Register status: decided (default, shadow input). Owner milestone: shadow input.

Two fixes from Jev and Clef on the 14 ORCA 6.1.1 fixture jobs (2026-10-03). (1) A fourth A66 check, root order: SC-NEVPT2 lists each (multiplicity, irrep) block's roots by corrected energy; when that order differs from the SA-CASSCF root order the broker reply says so with the printed eV values, tells the agent to identify states by character rather than position, and notes the CASSCF reference may describe them poorly. It compares printed decimals only, uses no reference value, and is advice like the other A66 checks (both arms get it). acetone_g_trah, a CASSCF solution different from QUESTDB's, is the fixture that trips it. (2) Shadow states (A67, version 2) add `scf_note` beside the parser's `scf`: when no SCF ran, they say so in plain words, naming MORead when the input echo uses it, because Jev read the bare `absent` as rule B (SCF failure) on 9 of 14 jobs. Clef reads its Workers AI token from `CLOUDFLARE_AI_TOKEN`; the environment's `CLOUDFLARE_API_TOKEN` is an account token Workers AI rejects.
