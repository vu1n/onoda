# Gate run 4: pre-registered checks

Written and committed before the run started (2026-10-07). The checks are those of
gates 1 to 3, unchanged. The full eight-molecule suite runs only if every check passes.

## Gate 3 result (2026-10-07, main at 07e8269)

Gate 3 failed check 1 (19 of 88 jobs failed, 22%) and check 2 (S1-thioacetone-r1
ended with no claim). Checks 3 and 4 passed. No run hit the 2-hour timeout; the
longest took 48 minutes. The 19 failed jobs:

- 8 CASSCF did not converge with ORCA's default orbital step; 7 of them still exited 0.
- 4 stopped with "Trafostep RI is set without a valid RI basis": `orca_input` wrote
  `trafostep ri` into jobs with `ri` off.
- 4 hand-edited inputs ORCA rejected, 2 other ORCA errors, 1 "NORB==0".

S1-thioacetone-r1 stopped after 33 requests and 5 jobs with an empty final message,
before it had called `report_vtes`. Thioacetone was accurate (13 states within 0.3 eV
over three runs); ketene was not (no state within 0.3 eV; matched states 1.4 to 5.6 eV
off). Outputs: Mac `experiments/questdb/runs/gate-3`.

## What changed since gate 3

- `orca_input` writes `trafostep ri` only with the RI basis (`ri` on).
- The project memory tier says to rerun a CASSCF that did not converge with
  `orbstep TRAH` from the same starting orbitals, reading convergence from the job
  reply's summary (memory-project@5).
- `REPORT_REMINDER=1`: an agent that stops without calling `report_vtes` gets one
  hidden message telling it to report what it identified. Both arms get it; it is
  recorded in the run's config and replays with the run.

Same grader (v3, 0.3 eV), model, caps, molecules and repeats.

## Run

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPORT_REMINDER=1 REPEATS=2 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" \
WORK=/tmp/qg4 experiments/questdb/run.sh
```

## Checks (all must pass)

1. **Failed jobs under 15%.** Pooled over both arms, `jobs_failed / jobs` from
   `onoda vte report` is below 0.15 (`job_failed` in `src/onoda/adapters/pi/vte.py`).
2. **Every run finishes.** All eight runs have an evaluation, and
   `runs_without_claim` is 0 in both arms.
3. **Graded by v3.** Every evaluation has `evaluator_version` 3 and
   `tolerance_ev` 0.3.
4. **No false credit from the grader.** `onoda vte audit` over the eight runs
   reports `false_accepts` 0.

Reported but not gated: coverage, accurate, MAE, false-success claims per arm,
`label_swap` acceptances from the audit, how many runs were reminded and whether they
then reported, `orbstep TRAH` use, wall time and cost.
