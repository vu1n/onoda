---
id: vte-report-process-metrics
project: onoda
type: decision
status: draft
title: "vte report shows per-arm process metrics: jobs per accurate state, failed-job share, cross-run stability"
related_code:
  - "src/onoda/adapters/pi/vte.py"
---

<!-- brief:anchor process-metrics -->
## Report how an arm got its result, beside what it got

Each arm in `onoda vte report` carries three descriptive process metrics next to coverage,
accuracy and MAE:

- `jobs_per_accurate`: the arm's broker jobs (cache hits included, as in `jobs`) divided by its
  `accurate` count, as decimal text; `unresolved` when nothing is accurate. It counts the
  agent's submissions, not compute: a cache hit is one call, like a job ORCA ran.
- `executed_per_accurate`: the same with only the jobs the arm's runs executed (`jobs_executed`,
  `executed_here`). This one does depend on the cache, and so on run order: for the same
  submissions, a warm cache executes fewer jobs.
- `jobs_failed_share`: `jobs_failed` divided by `jobs`, as decimal text; `unresolved` with no jobs.
  It is a share of jobs, not of runs, and says nothing about recovery.
- `stability`: per case, each run's `accurate` count in run order (`accurate_per_run`), its range
  (`accurate_spread`, max minus min), and the largest range over the arm's cases
  (`max_accurate_spread`). A case with fewer than two runs was not measured: its range is
  `unresolved`, never 0.

**Why.** The Nature Reviews Methods Primer *Agentic AI systems for scientific discovery*
(doi 10.1038/s43586-026-00534-y, Results and Outlook) asks agentic results to report cross-run
stability and efficiency, not only final-answer outcome. The noise run of 2026-10-08 showed that
identical code moves ketene's accurate count 0 to 4 per run while arm totals barely move, so the
per-case spread is what a reader needs to judge whether a gap between arms is noise.

**The measure.** The range of the accurate count is chosen because it needs no distributional
assumption and stays meaningful at 2 to 4 repeats, which is what batches have. The largest range
over cases is a one-number summary only; the per-case values are always shown with it. No
threshold is attached: nothing here says how large a spread is acceptable. The range describes
what was observed; it is not an uncertainty estimate, and equal counts can hide different
accurate states.

### Invariant
- These metrics are descriptive. They never enter a grade, a promotion gate or an arm verdict.
- `accurate` is the evaluator v3 count under `vte-energy-tolerance`; nothing new is graded.
- A run whose evaluation predates evaluator v3 has no accuracy, so its case's `accurate_spread`,
  the arm's `max_accurate_spread` and the arm's `jobs_per_accurate` and `executed_per_accurate`
  are `unresolved`, never
  computed from a stand-in zero.
- Values are integers or decimal text, never floats (`record-integrity`).
