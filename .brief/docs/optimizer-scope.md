---
id: optimizer-scope
project: onoda
type: decision
status: active
title: "The optimizer changes only allowlisted components, sees verdict classes only, and the seed moves only by promotion"
related_code:
  - "src/onoda/optimize/adapter.py"
  - "src/onoda/optimize/loop.py"
  - "src/onoda/optimize/compare.py"
  - "src/onoda/optimize/reflection.py"
  - "src/onoda/adapters/pi/contract.py"
  - "src/onoda/adapters/pi/seed_candidate.json"
carries: [A44, A45, A46, A48, A51, A53, A54, A55]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor seed-by-promotion-only -->
## Change only allowlisted components, and promote a seed only through the gates

GEPA proposals may change only the `HarnessCandidate` components in `COMPONENT_VALIDATORS`; the evaluator, parsers, gateway, budgets and splits are not components. Trials run on the acceptance slice only. Reflection sees agent-visible traces plus verdict words and reason classes, never reducer receipts, reference values or parsed numbers. A candidate becomes the seed only through `onoda optimize compare --live` passing the gates; `seed_candidate.json` holds exactly the measured bytes and its digest is pinned in `SEED_DIGEST`.

**Why.** An optimizer that could edit its judge, read the answers or swap the seed without a gate check would optimize the measurement instead of the harness.

### Invariant
- A proposal naming any other component, or failing a validator, is recorded `invalid` and never run.
- `OnodaAdapter.evaluate` refuses anything but the acceptance slice.
- `Trial` refuses a reason outside `REASON_CLASSES`; only `AGENT_VISIBLE_SOURCES` steps reach reflection.
- Import fails if `seed_candidate.json`'s digest is not `SEED_DIGEST`; any edit, even a typo fix, is a new candidate.
- GEPA's run-dir state (`gepa_state.bin`) is scratch: never imported, exported or treated as authoritative.
- A split over `pending-human-review` cases is `exploratory` and nothing selected on it is called validated.
- RLM children draw from the parent's request and cost budget; `rlm()` past depth, children or budget is refused before spawn.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

#### A44: Who pays for an `rlm()` child's requests?

Register status: decided. Owner milestone: M4.

The parent. A child's `max_requests` is the parent's remaining budget at spawn, and the child's spooled requests are charged to the parent afterwards, so the run's cap covers the whole tree. A call past the budget, max depth, or max children is refused before any process starts and spooled as `rlm.child.refused`.

#### A45: What may the optimizer change?

Register status: decided. Owner milestone: M4.

Only the `HarnessCandidate` components in `contract.COMPONENT_VALIDATORS`: the RLM system prompt, tool doc, child prompt template, helper library, limits, the advisory ladder, and SoL-Pi's four mechanism booleans. The evaluator, parsers, gateway, budgets, and splits are not components. A proposal naming anything else, or failing a component's validator, is recorded `invalid` and never run. The helper validator's import check is a lint that catches honest mistakes, not a boundary: helper code runs with the kernel's privileges (A38). Seed limits (depth 1, 4 children, 10000 output chars) are defaults, not tuned values.

#### A46: Is GEPA's run-dir state part of the record?

Register status: decided. Owner milestone: M4.

No. `gepa_state.bin` (pickled by GEPA) and anything else GEPA writes under its run dir is operational scratch for resuming a search: never imported into the store, exported, or treated as authoritative. GEPA itself unpickles it when a search resumes, so it is trusted like any local file of the operator's and must not be writable by trial agents once they are sandboxed (A38). The optimizer's record is its `CandidateRecord`s and outcome vectors in the store.

#### A48: Can the six pending-review cases carry an acceptance/final split?

Register status: assumed: exploratory only. Owner milestone: M4.

A `SplitAssignment` over cases that are `pending-human-review` (A26) is `exploratory: true`, and nothing selected on it may be described as a validated improvement until the cases are reviewed and the split is re-derived.

#### A51: How does a live optimizer search run and what may it spend?

Register status: decided. Owner milestone: live optimizer.

`onoda optimize run --live` takes every agent setting (provider, model, credential env, per-run `max_cost_usd`) and every reflection setting (provider `openrouter`, model, credential env, `max_tokens`, per-call `max_cost_usd`) plus a search-wide `--search-max-cost-usd` explicitly; nothing defaults and it refuses to mix with scripts. Trials run sandboxed (A50). Reflection is one OpenRouter chat completion per call with usage accounting on; each call, refused or not, is journalled as `optimizer.reflection-call` with its prompt, reply and a receipt (request without the credential, raw response, served model, tokens, cost as decimal text). A reply without a cost figure, above its per-call cap, cut off at `max_tokens`, empty, or not HTTP 200 is refused (GEPA then treats it as no proposal and moves on; the call is still charged). Spend accounting: a trial is reserved at its per-run cap and settles at its recorded cost; a reflection call is charged its receipt cost, or its reserve when it has none; the search stops before an iteration whose worst case (its trials and reflections at their reserves) the remaining cap cannot cover, and refuses a seed evaluation it cannot cover. Which model to use is the operator's choice, not a tuned value. A search over the six pending-review cases is exploratory (A48); its results are live model runs, not solver runs.

#### A53: What may reflection learn from the evaluator?

Register status: decided (Vu, 2026-09-27). Owner milestone: live optimizer.

Per claim tag, the verdict word and the evaluator's reason class (today only `unit`, from `state.<tag>.reason`), each with a fixed one-line meaning of the grading rule (`VERDICT_MEANINGS`, `REASON_CLASSES` in `optimize/adapter.py`). Never a reference value, parsed number or output excerpt. `Trial` refuses a reason outside `REASON_CLASSES`. In live2 every false-success claim was on the two TD-DFT cases (wrong unit; triplet roots the output does not have), and reflection, seeing only `contradicted`/`unsupported`, could not tell the causes apart. With repeats, reflection gets one trace per case (the lowest-scoring run's, to stay within the model's context) and every run's verdict line.

#### A54: How does a candidate become the seed?

Register status: decided (Vu, 2026-10-02, "Promote"). Owner milestone: promotion.

Only through the gates: `onoda optimize compare --live` runs the current seed and the candidate head to head on the split's acceptance slice (`--repeats` each, whole rounds under a spend cap, sandboxed) and judges the candidate with `gate_decision` and `gates/gate-spec.yaml`; any failed run leaves it unpromoted. The check is stored in the evaluator store (`promotion-check`). A passing candidate's components are copied verbatim into `src/onoda/adapters/pi/seed_candidate.json`, whose digest is pinned in code (`SEED_DIGEST`); an edit, even a typo fix, is a new candidate. The previous seed stays as `INITIAL_SEED_CANDIDATE`, and optimizer loop tests keep using it because their scripted reflections edit its texts. The first promotion is live3's 783ec4ea over the initial seed c76c4d0b (check `promote-live3`, 2026-10-02, deepseek-v4-flash, 5 acceptance cases x 2 runs each, $0.13): supported claims 39 -> 54, false success 15 -> 0, context bytes +3%, accepted on the capability gain. Exploratory (A48): the acceptance cases are unreviewed.

#### A55: How does an ablation run on the RLM harness?

Register status: decided. Owner milestone: promotion.

`rlm: true` in an ablation spec runs every arm as S1r with the seed candidate, each arm's mechanisms set through the candidate's `sol_pi.config` (S1 runs the seed as is). Arm names keep their mechanism meaning; `outcome_vector` accepts an S1r run as arm X only when the mechanisms it ran are exactly X's. S0 cannot be an arm (S1r needs the ladder), and S1r itself is still refused as an arm name. RLM children draw from the parent's request and cost caps (A44), so per-run caps cover the whole tree.
