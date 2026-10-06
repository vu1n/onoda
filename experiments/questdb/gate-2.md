# Gate run 2: pre-registered checks

Written and committed before the run started (2026-10-06). The checks are gate 1's,
unchanged. The full eight-molecule suite runs only if every check passes.

## Gate 1 result (2026-10-06, main at 6577304)

Gate 1 failed check 1: 12 of 50 ORCA jobs failed (24%; qv5 was 47%). Checks 2 to 4
passed. 11 of the 12 failures were in one run, S0-thioacetone-r1, which used all
24 of its jobs. Each of those 11 was an input error that failed at once: 8 were
parse errors on hand-written state weights in `%casscf`, and 3 were "Unknown
identifier in CASSCF block". The other three runs had 1 failure in 26 jobs.
Outputs: Mac `experiments/questdb/runs/gate-1`.

## What changed since gate 1

- The project memory tier and the `orca_input` description both say not to
  hand-write state weights (memory-project@3). Without them ORCA weights each
  block equally, not each root; the host check warns when roots end up unequal.

Nothing else changed: same grader (v3, 0.3 eV), tools, model and caps.

## Run

The same two molecules, both arms, one repeat each: four runs, four at a time.

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPEATS=1 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" WORK=/tmp/qg2 \
  experiments/questdb/run.sh
```

## Checks (all must pass)

1. **Failed jobs under 15%.** Pooled over both arms, `jobs_failed / jobs` from
   `onoda vte report` is below 0.15 (`job_failed` in `src/onoda/adapters/pi/vte.py`).
2. **Every run finishes.** All four runs have an evaluation, and
   `runs_without_claim` is 0 in both arms.
3. **Graded by v3.** Every evaluation has `evaluator_version` 3 and
   `tolerance_ev` 0.3.
4. **No false credit from the grader.** `onoda vte audit` over the four runs
   reports `false_accepts` 0.

Reported but not gated: coverage, accurate, MAE, false-success claims per arm,
`label_swap` acceptances from the audit, wall time and cost. With one run per
molecule and arm, one agent's choices can decide check 1; gate 1 showed that.
