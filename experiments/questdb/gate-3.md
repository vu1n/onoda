# Gate run 3: pre-registered checks

Written and committed before the run started (2026-10-07). The checks are gate 1's
and gate 2's, unchanged. The full eight-molecule suite runs only if every check passes.

## Gate 2 result (2026-10-06, main at bf93f0c)

Gate 2 failed check 1 (8 of 47 jobs failed, 17%) and check 2 (S0-thioacetone-r1
reached the 2-hour run timeout with no claim). Checks 3 and 4 passed. No job failed
on weight syntax. The 8 failed jobs had four causes:

- 3 stopped at once with "NORB==0 is invalid": a requested (mult, irrep) block had no
  active orbital of a symmetry that could make it (three B1 and two B2 active, no A1).
  ORCA exited 0. A host rerun without `bweight` failed the same way.
- 3 hand-edited inputs ORCA rejected: two nested per-irrep `nact` blocks, one broken
  `rotate`.
- 1 single-root CASSCF(6,4) in cc-pVDZ with `orbstep TRAH` did not converge.
- 1 single-root CASSCF(6,4) in aug-cc-pVTZ with `orbstep TRAH` was killed at the
  30-minute job limit.

S0-thioacetone-r1's two hours went into three such ground-state jobs with TRAH (26, 25
and 30 minutes). A host rerun of one of them without `orbstep TRAH` converged in 93 s,
at an energy 0.12 Eh lower. Outputs: Mac `experiments/questdb/runs/gate-2`.

## What changed since gate 2

- `orca_input` writes `orbstep TRAH` only when asked (`trah`); ORCA's default orbital
  step is the default. The project memory tier says to start with the default and use
  TRAH only when it fails (memory-project@4).
- The `orca_input` description and the memory tier say each (mult, irrep) block needs
  active orbitals that can make that symmetry, and that ORCA takes no per-irrep
  active-orbital counts.
- Two repeats per molecule and arm instead of one, so that one agent's choices weigh
  less in the pooled checks. The runs go four at a time, as before.

Same grader (v3, 0.3 eV), model and caps.

## Run

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPEATS=2 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" WORK=/tmp/qg3 \
  experiments/questdb/run.sh
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
`label_swap` acceptances from the audit, `orbstep TRAH` use, wall time and cost.
