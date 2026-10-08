# Gate run 6: pre-registered checks

Written and committed before the run started (2026-10-08). Gate 6 tests the
active-space guidance in memory-project@7 on the two molecules the gates have
used, before any full suite uses it.

## Suite 1 result (2026-10-07, main at 896ffdd)

All 32 runs plus one provider-error rerun (S0-diazomethane-r2, which then
settled with a claim but identified 0 of 7). 6 h 47 min, about $2.73.
False-success claims 0 in both arms; `onoda vte audit` false_accepts 0 over 193
states. S0 identified 51 of 106 target states, 37 within 0.3 eV, common-target
MAE 1.015 eV; S1 46 of 106, 32 within 0.3 eV, 1.052 eV. Runs without a claim: S1-acetone-r2 hit the
2-hour limit; S0-methylenecyclopropene-r2 settled without reporting after the
report reminder. The reminder fired in 3 runs (its first live firing). Outputs:
Mac `experiments/questdb/runs/suite-1` and `runs/suite-1-rerun-S0-diazomethane-r2`.

Ketene and thioacetone in suite 1 (found / within 0.3 eV, both repeats pooled,
14 target slots per molecule and arm):

| | S0 | S1 |
|---|---|---|
| ketene | 6 / 2 | 6 / 2 |
| thioacetone | 9 / 8 | 6 / 6 |
| both | 15 / 10 | 12 / 8 |

## What changed since suite 1

memory-project@7 adds "One active space per group of targets": a separate
state-averaged job per target group whose active space holds the orbitals that
group excites into; check which orbitals are active, not only how many per
irrep; one extra root in the singlet block that holds the ground state; average
only the states one group needs, with triplet jobs still leading with the
singlet ground state. It came from the wrong-state dig over gates 3 to 5 (about
70 of 188 claims came from an active space without the orbital the target
needed).

Same grader (v3, 0.3 eV), model, caps, molecules, repeats and report reminder
as gate 5.

## Run

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPORT_REMINDER=1 REPEATS=2 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" \
WORK=/tmp/qg7 experiments/questdb/run.sh
```

A run that ends with status `provider-error` is rerun once into a separate
folder with only `WORK=/tmp/qg7r ONLY_RUNS="<run id>"` changed; the rerun
replaces it in check 2. A second `provider-error` for the same run fails check 2.

## Checks (all must pass)

Checks 1 to 4 are gate 5's: the guidance must not break the harness.

1. **Refused inputs under 15%.** Pooled over both arms, `jobs_rejected / jobs`
   from `onoda vte report` is below 0.15.
2. **Every run finishes.** All eight runs have an evaluation, and
   `runs_without_claim` is 0 in both arms (after any provider-error rerun).
3. **Graded by v3.** Every evaluation has `evaluator_version` 3 and
   `tolerance_ev` 0.3.
4. **No false credit from the grader.** `onoda vte audit` over the eight runs
   reports `false_accepts` 0.

## Reported against suite 1, not gated

- Within 0.3 eV per arm on ketene and thioacetone, against the table above
  (S0 10 of 28, S1 8 of 28). With two repeats the run-to-run spread is large, so
  a change is described with its spread, not as a result.
- Whether agents followed the guidance, from the inputs alone: runs whose cited
  SC-NEVPT2 jobs use more than one active space (gates 3 to 5: none), and singlet
  ground-state blocks with fewer roots than the target count plus one (gates 3
  to 5: 9 of 13 thioacetone runs).
- Coverage, MAE, false-success claims, `jobs_failed / jobs`, `label_swap`
  acceptances, provider errors and reruns, reminders, wall time and cost.
