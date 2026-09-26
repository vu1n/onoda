# Experiment design and scientific reconstruction

## 1. Three labels, not one reproduction claim

Use **paper-inspired reconstruction** while the exact task membership, information package, attempt semantics, and evaluation are unresolved. Use **matched internal comparison** when our variants share a frozen environment and evaluator. Reserve **aligned reproduction** for runs whose documented differences from the paper have been reconciled sufficiently for the specific claim.

Keep a reconstruction matrix with columns: parameter, source location, published description, our implementation, uncertainty, validation evidence, and remaining difference. The initial source facts are in [SOURCE_NOTES.md](SOURCE_NOTES.md).

Do not substitute a database's strongest reference value for the method-specific reference required by the experiment. Preserve method/basis/protocol metadata at import and reject ambiguous selections. Never infer that the latest whole database equals the paper's historical curated subset.

## 2. Data preparation and allowed information

Pin the dataset revision and all source files. Build a machine-readable manifest with stable molecule, geometry, transition, and grouping IDs. Preserve original bytes and source links. Record exclusions before observing agent performance.

Prepare two views:

- **Agent:** geometry, the allowed task objective, permitted software documentation and curated precedents, budgets, and any context explicitly allowed in the information regime.
- **Evaluator:** references, target inventory, state-assignment information, rubric, and private adjudication material.

Do not leak task-specific protocols or reference energies through filenames, sample solutions, search indexes, sibling experiment artifacts, gateway errors, or global object lookup. In a full-information replication track, disclose additional protocols deliberately and record the regime change. Do not mix its outcomes into the restricted-information leaderboard.

Use frozen document retrieval or an explicit logged allowlist. Unrestricted browsing introduces both information-control and reproducibility problems. If the original literature package cannot be recreated, record the replacement as a material difference. Instructions to ignore pretrained knowledge are not proof that contamination is absent.

## 3. Development sequence

**A. Infrastructure fixtures.** Invented data tests graph behavior, storage, budgets, and replay. Clearly label it synthetic. It cannot validate chemistry, parser correctness on authentic outputs, or provider performance.

**B. Fixed-input scientific smoke tests.** Select a few tractable documented calculations; inspect settings, output parsing, state matching, and implementation differences. Use authentic outputs and explicit provenance. These become development/integration cases, not untouched test evidence.

**C. Prime baseline.** On a small frozen development set, compare general documentation with our reconstruction of the advisory ladder. Use the same reasoning model, tools, permitted information, resource limits, and evaluator. Missing scientific checks remain unresolved.

**D. Individual treatments.** Test Jev, memory, and GEPA separately, then combine promising components. Run fresh full trajectories in addition to branch-local probes.

## 4. Experimental variants

| ID | Variant | Isolated question |
|---|---|---|
| P0 | Prime + allowed documentation, no reconstructed ladder | Internal baseline. |
| P1 | Same Prime/model/tools + advisory ladder | Does formalized procedure help in our environment? |
| P2 | P1 + executable approved branches, reasoning model still supplies bounded decisions | Does changing control flow help, independent of Jev? |
| P3 | P2 + Jev replacing specified decision calls | Does the decision provider improve the quality/cost tradeoff? |
| P4 | P1 + frozen case memory | Does prior permitted experience help unseen cases? |
| P5 | P1 + GEPA-optimized ladder, no added memory | Does rule optimization generalize? |
| P6 | Validated combination of treatments | Do useful components remain useful together? |
| R0 | Full-information fixed-protocol replication track | Can the system execute supplied workflows correctly? |

If P2 is initially omitted for budget reasons, label P3 as a combined control-flow/provider treatment. Do not attribute all its effect to Jev. A future original-harness baseline is optional, not a blocker; use the authors' latest human-improved procedure if supplied and keep its provenance distinct from the published reconstruction.

Record resolved model/provider identifiers where available and freeze global harness state. If an old model snapshot cannot be obtained, compare current variants fairly without implying historical model equivalence.

## 5. Splits and optimization

Split by molecule, and where feasible by scientifically relevant family. Keep related configurations and transitions together. Assign development, validation, and untouched test groups before optimization. Save the assignment and random seed as immutable artifacts.

GEPA may inspect development outcomes and a defined validation process, not test outcomes. Frozen memory comes from eligible development experiences only. Memory, policies, prompts, retrieval code, tool code, and evaluator versions are pinned before test execution.

A separate continual-learning track may permit adding a case only after it has been evaluated, with time-ordered visibility and frozen adjudication. Never mix that track with a frozen-memory result under the same label.

Repeated held-out inspection is adaptive evaluation. Track such access and reserve a genuinely untouched final group or label results exploratory.

## 6. Outcome definitions

Report a vector, not a single unsupported success score:

| Measure | Definition |
|---|---|
| Execution completion | Jobs finish with required artifacts; not a scientific verdict. |
| Identified-state coverage | Unambiguously matched targets divided by the fixed eligible target count. |
| Scientific-check status | Pass/fail/unresolved per declared check, with supporting evidence. |
| Reference error | Signed/absolute error and aggregate metrics over identified targets; always show denominator. |
| Common-target error | Error on targets identified by both compared variants, to expose selection effects. |
| False-success claims | Agent claims success but independent evaluation rejects or cannot support it. |
| Resource consumption | Actual inference, solver, retry, elapsed, and queue consumption with missingness disclosed. |
| Reproducibility | Step-level replay/reanalysis/reexecution capability and observed agreement. |

Keep unsuccessful and ambiguous targets in the coverage denominator. Infrastructure failures should be classified and costed, not silently dropped. Predeclare eligibility changes; show their effect rather than retrospectively narrowing the task.

Reference agreement is not an experimental measurement or universal proof of chemical correctness. Do not impose a made-up accuracy tolerance to turn continuous energy error into success. If a thresholded outcome is needed, choose and justify it before comparing candidates. Numerical repeatability tolerance is a separate setting.

An evaluator update produces new scores for both variants on frozen outputs. Preserve historical scores. The authors' newer unpublished expert check is not implemented until its evidence requirements and decision rule are understood.

## 7. Budget matching and uncertainty

Use resource ceilings plus actual consumption. At minimum record inference spending, solver CPU/resource usage, job/attempt counts, and elapsed time; record source pricing or leave money unknown. Do not treat a calculation count as an equal-cost unit.

Account for canceled jobs, failed requests, recursive subagents, retrieval, memory construction, candidate evaluation, and optimization. Separate execution cost from one-time improvement costs and state the amortization assumption for any economic claim.

Repeat trials where affordable. Estimate uncertainty at the molecule/group level, not by pretending all transitions from one molecule are independent. Do not choose a repetition count or statistical conclusion that the available budget cannot support; publish raw outcomes and limitations.

## 8. Fork experiments

A useful branch-local experiment starts from one completed diagnostic checkpoint and evaluates several next-action policies on exactly the permitted evidence available then. Each branch executes its changed action and required suffix independently.

This measures conditional recovery quality. It does not prove an end-to-end policy would reach the same checkpoint or perform better from task initialization. Report parent-selection criteria and all attempted branches, not just successes.

Use identical remaining budgets for a matched branch comparison. Show inherited, marginal, full-path, and actual aggregate spending as defined in the portability contract. Do not use the original run's later successful answer to design a supposedly blind branch.

## 9. Promotion criteria

Before a component is promoted, it must preserve required scientific checks, pass leakage and reproducibility tests, and demonstrate a useful tradeoff under the declared budget. No preselected percentage improvement is promised.

First technical milestone: a trustworthy portable run. First scientific milestone: fixed-input results and evaluator behavior we can explain. First agent milestone: an internally valid controlled comparison. Only then pursue a broad benchmark or training loop.
