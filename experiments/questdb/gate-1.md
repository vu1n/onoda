# Gate run 1: pre-registered checks

Written and committed before the run started (2026-10-06). The full eight-molecule
suite runs only if every check below passes. A check is not changed after the run
has been seen; if a check turns out to be wrong, that goes in a new gate.

## What changed since qv5

- Grader v3 (#30, vte-grading-contract@0002): a claim is matched to a state only
  by spin, irrep and character, and each matched state is judged within 0.3 eV
  (draft decision vte-energy-tolerance).
- `orca_input` (#33): both arms can build ORCA input text from chosen parameters.
- Project memory gains the ORCA 6.1.1 input shapes that ran (#31).
- Runs go in parallel, one store each (#32).

## Run

Two molecules, both arms, one repeat each: four runs, four at a time (16 cores).

- ketene: the five-step route is known to work here (fixtures `ketene_*`).
- thioacetone: S1's five false successes in qv5 all came from this molecule.

Model and caps are the same as qv5, so only the changes above differ:

```
ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter MODEL=deepseek/deepseek-v4-flash \
CREDENTIAL_ENV=OPENROUTER_API_KEY MAX_REQUESTS=200 MAX_COST_USD=1 TIMEOUT=7200 \
REPEATS=1 PARALLEL=4 CASES="questdb-ketene questdb-thioacetone" WORK=/tmp/qg1 \
  experiments/questdb/run.sh
```

## Checks (all must pass)

1. **Failed jobs under 15%.** Pooled over both arms, `jobs_failed / jobs` from
   `onoda vte report` is below 0.15. A job counts as failed when the broker
   reports it failed, or the parser finds no normal termination or a CASSCF
   that did not converge (`job_failed` in `src/onoda/adapters/pi/vte.py`).
   qv5 was 47%.
2. **Every run finishes.** All four runs have an evaluation, and
   `runs_without_claim` is 0 in both arms.
3. **Graded by v3.** Every evaluation has `evaluator_version` 3 and
   `tolerance_ev` 0.3.
4. **No false credit from the grader.** `onoda vte audit` over the four runs
   reports `false_accepts` 0.

Reported but not gated: coverage, accurate, MAE, false-success claims per arm,
`label_swap` acceptances from the audit, wall time and cost. Four runs are too
few to compare the arms.
