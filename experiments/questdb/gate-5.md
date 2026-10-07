# Gate run 5: pre-registered checks

Written and committed before the run started (2026-10-07). The full eight-molecule
suite runs only if every check passes. Check 1 changes from gate 4 (see "What
changed"); checks 2 to 4 are gate 4's.

## Gate 4 result (2026-10-07, main at 14278fc)

Gate 4 ran twice with the same command and code.

- First attempt (`/tmp/qg4`, 93 min, $0.43): check 1 passed (7 of 66 jobs failed,
  10.6%), check 2 failed, checks 3 and 4 passed. The two runs without a claim,
  S0-thioacetone-r2 and S1-thioacetone-r1, ended with status `provider-error`
  ("Connection error", "terminated") while the Mac's connection dropped.
- Rerun of all eight (`/tmp/qg5`, 84 min, $0.42): check 1 failed (11 of 68, 16.2%),
  checks 2 to 4 passed. No provider errors.

Outputs: Mac `experiments/questdb/runs/gate-4` and `runs/gate-4-rerun`.

The 18 failed jobs of both attempts (134 jobs), read from their inputs and outputs:

- 12 CASSCF jobs did not converge with ORCA's default orbital step (four of them
  exit with a bare "ERROR" after 150 iterations). The memory tier prescribes this
  order: the default step first, `orbstep TRAH` when it fails. In 9 of the 12 a
  later job of the same setup with TRAH converged; the other 3 were fixed by
  changing the active space or the blocks. Six are thioacetone, where the default
  step diverges in aug-cc-pVTZ and runs 7 to 20 minutes before failing.
- 1 TRAH job was killed at the 30-minute job limit.
- 3 hand-written inputs kept `trafostep ri` but dropped `RIJCOSX AutoAux`
  ("Trafostep RI is set without a valid RI basis"); `orca_input` cannot write that.
- 1 triplet block under a singlet multiplicity line, 1 input that stopped at once
  and was fixed by removing two hydrogen lines.

So 13 of the 18 iterated and then failed, which is the prescribed fallback at work,
and 5 (3.7% of jobs) were inputs ORCA refused. Gate 1's 15% rule was written after
qv5, where 47% of jobs failed on input syntax; the failed-job count now mostly
measures how often the default step fails on a molecule, which swings from run to
run (10.6% and 16.2% from the same code).

Per arm over the rerun (reported, not gated): S0 identified 11 of 28 states, 6
within 0.3 eV, MAE 1.35 eV; S1 17 of 28, 11 within 0.3 eV, MAE 0.49 eV; no false
success claims. Two molecules and four runs per arm: not a result about the ladder.
The report reminder fired in neither attempt.

## What changed since gate 4

- Check 1 counts inputs ORCA refused, not every failed job: a failed job counts
  only if it stopped before any SCF or CASSCF iteration (`jobs_rejected` in
  `onoda vte report`, `job_input_rejected` in `src/onoda/adapters/pi/vte.py`). Jobs
  that iterated and then failed (no convergence, divergence, the time limit) are
  reported as `jobs_failed` and not gated. Vu chose this on 2026-10-07.
- The memory tier says `trafostep ri` needs `RIJCOSX AutoAux` on the `!` line and
  to drop the two together or let `orca_input` build the input
  (memory-project@6).
- A run that ends with status `provider-error` (the model provider's connection
  failed) is rerun once with the same command into a separate OUT folder; that
  rerun replaces it in check 2, and the original keeps its record and its jobs
  still count in check 1. A second `provider-error` for the same run fails check 2.

Same grader (v3, 0.3 eV), model, caps, molecules, repeats and report reminder.

## Run

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPORT_REMINDER=1 REPEATS=2 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" \
WORK=/tmp/qg6 experiments/questdb/run.sh
```

## Checks (all must pass)

1. **Refused inputs under 15%.** Pooled over both arms, `jobs_rejected / jobs` from
   `onoda vte report` is below 0.15.
2. **Every run finishes.** All eight runs have an evaluation, and
   `runs_without_claim` is 0 in both arms (after the provider-error rerun above,
   if one was needed).
3. **Graded by v3.** Every evaluation has `evaluator_version` 3 and
   `tolerance_ev` 0.3.
4. **No false credit from the grader.** `onoda vte audit` over the eight runs
   reports `false_accepts` 0.

Reported but not gated: `jobs_failed / jobs` (gate 4's check 1), how many failed
jobs were followed by a converged TRAH retry, coverage, accurate, MAE,
false-success claims per arm, `label_swap` acceptances from the audit, provider
errors and reruns, how many runs were reminded and whether they then reported,
wall time and cost.
