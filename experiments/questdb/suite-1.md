# Full suite 1: pre-registration

Written and committed before the run started (2026-10-07). This is the full
eight-molecule S0-vs-S1 run that a passing gate unlocks (gate 5 passed).

## Gate 5 result (2026-10-07, main at 1329f72)

All four checks passed: 0 of 82 job inputs refused by ORCA (2 of 98 counting the
rerun's jobs), every run reported a claim, every evaluation graded by v3 at 0.3 eV,
and the audit found no false accepts. S1-thioacetone-r1 ended with `provider-error`
("Request timed out") and its one rerun, under the gate's rule, reported a claim
10 minutes before its 2-hour limit.

Reported, not gated: 13 of 98 jobs failed (6 followed by a converged TRAH retry);
S0 identified 11 of 28 states, 5 within 0.3 eV; S1 8 of 28, 6 within 0.3 eV; no
false success claims; the report reminder did not fire. 11 of 56 target states came
out within tolerance and ketene was poor in three of four runs, so the gate says the
harness is ready, not that the chemistry is good. Cost $0.64; the eight runs took
56 minutes, the rerun 110. Outputs: Mac `experiments/questdb/runs/gate-5` and
`runs/gate-5-rerun-S1-thioacetone-r1`.

## Run

The gate 5 command over all eight cases (run.sh's default `CASES`):

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPORT_REMINDER=1 REPEATS=2 PARALLEL=4 WORK=/tmp/qs1 experiments/questdb/run.sh
```

32 runs: acetaldehyde, formamide, ketene, diazomethane, acetone, thioacetone,
methylenecyclopropene and cyclopropenone, two arms, two repeats each.

## Rules fixed before the run

- A run that ends with status `provider-error` is rerun once with the same settings
  into a separate folder, and the rerun replaces it in the results; the original
  keeps its record. The rerun is the command above with only these changed, so it
  runs that one run and nothing else (for example S1-thioacetone-r1):
  `WORK=/tmp/qs1r ONLY_RUNS="S1-thioacetone-r1"`. A second `provider-error` for the same run is reported
  as a run without a claim. No other run is rerun: a run that hits the 2-hour limit
  or stops without a claim stays as it is.
- Nothing about the run (code, memory tiers, caps, model, cases) changes once it
  has started. If it has to stop, it resumes from the same pinned commit and OUT
  folder (run.sh skips runs already graded).

## Reported

`onoda vte report` over all 32 runs (after any provider-error reruns) and
`onoda vte audit`: per arm, coverage, accurate (within 0.3 eV), MAE,
common-target MAE, false-success claims, runs without a claim, `jobs_rejected`
and `jobs_failed`; the audit's `false_accepts` and `label_swap` acceptances; per
molecule, the same counts; provider errors and reruns, runs that hit the time
limit, whether the report reminder fired, wall time and cost.

The report counts and does not declare an arm better (`vte_report`'s note). With
two repeats per molecule and arm, and run-to-run spread as large as the gates
showed, a difference between arms is described with its spread, not as a result
about the ladder.
