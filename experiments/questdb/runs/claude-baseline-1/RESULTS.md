# Claude baseline 1: Opus 5.5 and Sonnet 5.5 on the QUESTDB slice (S1)

Exploratory development work, not a chemistry result.

**Opus 5.5 is accurate on 32 of 56 target slots, against DeepSeek's 22 of 56
(range 21-23): +10, outside the 5-slot noise band. Sonnet 5.5 is accurate on
23 of 56: +1, a tie with DeepSeek.** Opus beats Sonnet by 9 slots.

| Model | Accurate within 0.3 eV | Found | False successes | Runs without claim | Audit false accepts | Billed | Per run | Per accurate state |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek v4 flash (reference) | 22/56 (21-23) | not re-aggregated | | | | | | |
| Claude Opus 5.5, effort high | 32/56 | 36/56 | 0 | 0 | 0 of 56 states | $12.23 | $1.53 | $0.38 |
| Claude Sonnet 5.5, effort high | 23/56 | 30/56 | 0 | 0 | 1 of 50 states | $5.06 | $0.63 | $0.22 |

Total billed for the task: **$17.29** of a $100 ceiling (OpenRouter
billed-key counter, $96.4607 before, $113.7490 after). Other use of the key in
that window was not ruled out, but Pi's own per-response estimates sum to
$12.23 (Opus) and $5.06 (Sonnet), matching the counter deltas to the cent.

## What the numbers say

- **Coverage and accuracy, separately.** Opus found 36 and was accurate on 32
  (4 found-but-inaccurate, all thioacetone). Sonnet found 30 and was accurate on
  23 (7 found-but-inaccurate).
- **By molecule (accurate of 28).** Opus: ketene 20, thioacetone 12.
  Sonnet: ketene 9, thioacetone 14. The two models are
  strong on opposite molecules: Opus is 5 of 7 on ketene in every run and 3 of 7
  on thioacetone in every run; Sonnet is 2-4 of 7 on thioacetone and 1-3 of 7 on ketene.
- **Run-to-run spread.** Opus gave the same found and accurate count in all four
  repeats of each molecule. Sonnet varied (ketene 1-3 accurate, thioacetone 2-4).
- **Effort spent.** Opus: 93 ORCA jobs (9 failed, 1 rejected), 306 requests,
  13.1M tokens (188k output), 379 run-minutes. Sonnet: 101 jobs
  (16 failed, 7 rejected), 262 requests, 11.6M tokens (202k output),
  179 run-minutes. No run reached the 200-request cap, the 24-job cap or the
  2-hour timeout; every run settled with a claim.
- **Audit.** Opus: 0 false accepts over 56 claimed states. Sonnet: 1 false accept
  over 50 claimed states: in thioacetone r4, the claim `1 A1 pi,pi* 6.426 j0014` was
  still credited when pointed at another job (`other_job` variant, 1 of 523 graded).
  That is a grader observation, not a false-success claim by the agent. `label_swap`
  is measured only (Opus 18 of 52, Sonnet 20 of 46).
- MAE over found states (footnote only): Opus 0.171 eV, Sonnet 0.251 eV.

## Per run

### Opus 5.5

| Run | Found | Accurate | Claim | Jobs (failed) | Requests | Tokens (M) | Cost | Wall |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| ketene r1 | 5/7 | 5/7 | yes | 16 (1) | 46 | 2.34 | $1.63 | 29 min |
| ketene r2 | 5/7 | 5/7 | yes | 12 (0) | 37 | 1.30 | $0.99 | 20 min |
| ketene r3 | 5/7 | 5/7 | yes | 12 (3) | 40 | 1.65 | $1.12 | 29 min |
| ketene r4 | 5/7 | 5/7 | yes | 11 (0) | 43 | 1.99 | $1.26 | 17 min |
| thioacetone r1 | 4/7 | 3/7 | yes | 10 (0) | 32 | 1.16 | $1.24 | 59 min |
| thioacetone r2 | 4/7 | 3/7 | yes | 9 (1) | 33 | 1.27 | $1.89 | 72 min |
| thioacetone r3 | 4/7 | 3/7 | yes | 12 (3) | 41 | 1.70 | $1.68 | 71 min |
| thioacetone r4 | 4/7 | 3/7 | yes | 11 (1) | 34 | 1.72 | $2.43 | 82 min |

### Sonnet 5.5

| Run | Found | Accurate | Claim | Jobs (failed) | Requests | Tokens (M) | Cost | Wall |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| ketene r1 | 3/7 | 3/7 | yes | 13 (1) | 31 | 1.14 | $0.43 | 12 min |
| ketene r2 | 3/7 | 3/7 | yes | 10 (0) | 25 | 0.78 | $0.33 | 6 min |
| ketene r3 | 2/7 | 1/7 | yes | 21 (5) | 59 | 3.12 | $1.00 | 21 min |
| ketene r4 | 2/7 | 2/7 | yes | 23 (6) | 53 | 3.32 | $1.49 | 59 min |
| thioacetone r1 | 5/7 | 4/7 | yes | 5 (0) | 21 | 0.52 | $0.25 | 9 min |
| thioacetone r2 | 5/7 | 4/7 | yes | 6 (1) | 18 | 0.42 | $0.28 | 20 min |
| thioacetone r3 | 5/7 | 4/7 | yes | 4 (0) | 15 | 0.31 | $0.18 | 7 min |
| thioacetone r4 | 5/7 | 2/7 | yes | 19 (3) | 40 | 1.94 | $1.10 | 45 min |

Cost per run is Pi's estimate at the real OpenRouter prices; the batch totals
equal the billed counter deltas. Tokens are almost entirely cache reads.

## Protocol and deviations

- Code: `5df4a962e3bba1b69bfd35595db8150f7550b5c6` = origin/main `79d1f77` (no #46)
  plus one commit on branch `pi-models-json`. `memory-project@7`, ladder,
  parser and grader v3 unchanged. Cases ketene and thioacetone, arm S1, 4 repeats
  each, `MAX_REQUESTS=200 TIMEOUT=7200 REPORT_REMINDER=1 PARALLEL=4`, ORCA 6.1.1.
- **The harness commit.** The pinned Pi (pi-ai 0.85.1) has no catalog entry for
  either 5.5 model and would have served them through a clone of Kimi K2.6
  (wrong API type and prices; this is the stop recorded earlier in this file's
  history). Vu chose to add the models to Pi. `run-vte --pi-models` / `PI_MODELS`
  copies a Pi `models.json` into each run's agent dir and records its sha256
  (`92824d2f...`) in the run's config; unset, behaviour is unchanged. The entries
  copy Pi's own Opus 5 and Sonnet 5 OpenRouter entries (`anthropic-messages`) with
  the 5.5 prices ($4/$20/$0.20/$5 and $2/$10/$0.10/$2.50 per million).
- **Effort.** `MODEL=<id>:high`; Pi's sessions record `thinkingLevel: high`. The
  request body on the wire was not captured.
- **Cost caps.** Vu replaced the planned $5 per run and $40 total with no
  per-run limit and a $100 total. The harness requires a per-run number, so it was
  set to 100; the total was enforced by a watchdog on the billed counter. Neither
  was reached. The planned 4-run pilot stop rules were dropped with the caps.
- Sonnet started two hours after Opus finished (operator gap), not back to back.
- No provider errors, so no reruns.
- DeepSeek's reference is the prior 22/56 mean; it was not rerun here, and its
  found count, jobs and cost were not re-aggregated for this report.

Raw run stores, reports and audits are kept locally under
`experiments/questdb/runs/claude-baseline-1/` on the Mac (not committed).
