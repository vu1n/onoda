# ladder-opus-1: Opus 5.5, S0 (no ladder) vs S1 (ladder), QUESTDB slice (not committed)

Exploratory development work, not a chemistry result.

**Not a clean ladder benchmark.** Both arms carried the same frozen memory tiers (memory-harness@1, memory-project@7). Vu accepts the memory as part of onoda, but it means S0 is not a no-help baseline. Any S0/S1 difference here is the ladder's effect *on top of* the memory, not the ladder alone.

Run 2026-10-25 00:27Z–05:01Z on Vu's Mac. All runs used the pinned detached worktree `.pinned/8f945ab` (main, including PR #57's per-run ORCA job roots). Mac preflight there: 119 passed, 3 skipped (bubblewrap, Linux-only).

The settings are the same as fault-pilot-1 with no fault plan:
- `PROVIDER=anthropic MODEL=claude-opus-5-5:high CREDENTIAL_ENV=ANTHROPIC_API_KEY`
- `PI_MODELS=experiments/questdb/pi-models-claude-5.5-anthropic.json` (config `pi_models_sha256` 839b0eb4…, matching the file)
- `MAX_REQUESTS=200 MAX_COST_USD=6 TIMEOUT=7200 REPORT_REMINDER=1 PARALLEL=4 REPEATS=2`
- all eight cases; memory-harness and memory-project@7
- `ENABLE_LIVE_EXECUTION=1 WORK=/tmp/ql`

Everything ran detached under `caffeinate -i`.

**Stop reason: completed.** 32 of 32 runs settled with a claim. Total spend was **$41.39**, against the $60 hard stop. A watchdog that would kill the run past $60 was armed throughout.

## Headline
| arm | runs | found v3 | accurate v3 | found v4 | accurate v4 | jobs (failed) | false successes | MAE eV v3 / v4 | spend |
|---|---|---|---|---|---|---|---|---|---|
| S0 | 16 | 81/106 | 71 | 104/106 | 94 | 163 (16) | 0 | 0.177 / 0.160 | $17.25 |
| S1 | 16 | 80/106 | 66 | 104/106 | 90 | 192 (20) | 0 | 0.184 / 0.168 | $22.26 |

- "Accurate" means within 0.3 eV. The report's note reads: "counts and errors only; no arm is declared better."
- Common targets: v3 has 41, with MAE S0 0.176 and S1 0.185. v4 has 52, with MAE S0 0.160 and S1 0.168.
- Jobs per accurate state: v3 S0 2.30, S1 2.91; v4 S0 1.73, S1 2.13.
- Largest accurate-count spread between repeats: v3 S0 3, S1 1; v4 S0 0, S1 1.
- S1 used 29 more ORCA jobs and $5.01 more than S0, for coverage that is the same or one state lower.
- Wall time summed over runs: S0 425 min, S1 373 min.
- No run hit the request cap, the cost cap or the timeout.
- False successes: **0** in all 32 runs.
- `onoda vte audit` false_accepts: **2 under v3, 3 under v4**, over 212 claimed states. All are `other_job` variants in S1-diazomethane-r2: the claim, re-pointed at job j0009, was still credited. The v4 addition is `1 A1 pi,pi* 6.434 j0009`, a row v4 now identifies. `label_swap` is measured only: 64 of 163 under v3, 48 of 147 under v4.
- Grader v3 is the grade run.sh produced. Grader v4 is the regrade from `.pinned/ac27d4f` (PR #58), done after the runs. Both evaluations are kept in every evaluator store.

## Block check (after the first 8 runs)
The first 8 runs were acetaldehyde and formamide, S0/S1 × r1/r2. They ran 00:27–01:10Z.
- All 8 settled with a claim. No provider errors. 0 false successes.
- Scheduler collision scan: 82 executed jobs, 0 shared.
- Mean cost $0.88 per run.
- Result: valid, so I continued. Details are in BLOCK1.md; `report-block1.json` is the report over those 8.

## Provider errors and the one rerun
S1-cyclopropenone-r1 and S0-cyclopropenone-r2 both ended `provider-error` at 04:19–04:20Z, with 0 of 11 found. Each Pi session had 4 `Connection error.` stops at the same moment. I reran each once with `ONLY_RUNS` into a fresh `WORK=/tmp/ql-rerun`, with `OUT=ladder-opus-1-rerun`. Both settled at 9 of 11 found. Nothing else was rerun.

The pooled results above use the two reruns in place of the failed originals. The failed originals are excluded from the pool, but their $1.88 counts in total spend.

Note that `report.json` in this folder is run.sh's own end-of-run report, and it still pools the two provider-error originals. Use `regrade/v3/ladder-opus-1.report-arms.json` and `regrade/v4/…` instead.

## Validity
- Scheduler collision scan over all 34 broker journals (32 pooled runs plus the 2 failed originals) against their per-run roots `/tmp/ql*/jobs/<run>`: **378 executed jobs, 0 collisions**. In every root, the scheduler's execution count equals its executed jobs, which equals the journal's executed replies.
- No run.err has content. The only error stops in any Pi session are the two connection errors above.
- Spend is Pi's per-call `usage.cost.total`, summed over sessions. That meter reproduces fault-pilot-1's $20.56 to the cent. No provider billed-usage reading was possible: the key in `.env.local` is not an admin key.

## Per run
"Jobs (failed)" comes from `onoda vte report` run by run, and sums to the pooled counts. Wall time is from progress.log.

| run | status | found v3 | accurate v3 | found v4 | accurate v4 | jobs (failed) | false successes | cost | wall |
|---|---|---|---|---|---|---|---|---|---|
| S0-acetaldehyde-r1 | settled | 2/2 | 2 | 2/2 | 2 | 8 (2) | 0 | $0.58 | 14 min |
| S0-acetaldehyde-r2 | settled | 2/2 | 2 | 2/2 | 2 | 9 (2) | 0 | $0.65 | 15 min |
| S0-acetone-r1 | settled | 7/7 | 7 | 7/7 | 7 | 9 (1) | 0 | $1.49 | 28 min |
| S0-acetone-r2 | settled | 4/7 | 4 | 7/7 | 7 | 13 (1) | 0 | $1.96 | 81 min |
| S0-cyclopropenone-r1 | settled | 9/11 | 6 | 11/11 | 8 | 8 (0) | 0 | $1.20 | 21 min |
| S0-cyclopropenone-r2 (rerun) | settled | 9/11 | 6 | 11/11 | 8 | 12 (1) | 0 | $1.26 | 28 min |
| S0-diazomethane-r1 | settled | 5/7 | 5 | 7/7 | 7 | 10 (0) | 0 | $0.98 | 10 min |
| S0-diazomethane-r2 | settled | 5/7 | 5 | 7/7 | 7 | 11 (0) | 0 | $0.89 | 10 min |
| S0-formamide-r1 | settled | 6/6 | 5 | 6/6 | 5 | 13 (1) | 0 | $0.97 | 11 min |
| S0-formamide-r2 | settled | 6/6 | 5 | 6/6 | 5 | 13 (0) | 0 | $1.30 | 13 min |
| S0-ketene-r1 | settled | 5/7 | 5 | 7/7 | 7 | 12 (1) | 0 | $0.87 | 11 min |
| S0-ketene-r2 | settled | 5/7 | 5 | 7/7 | 7 | 11 (1) | 0 | $0.77 | 11 min |
| S0-methylenecyclopropene-r1 | settled | 4/6 | 4 | 5/6 | 5 | 13 (2) | 0 | $1.50 | 61 min |
| S0-methylenecyclopropene-r2 | settled | 4/6 | 4 | 5/6 | 5 | 11 (3) | 0 | $1.08 | 52 min |
| S0-thioacetone-r1 | settled | 4/7 | 3 | 7/7 | 6 | 7 (1) | 0 | $1.40 | 52 min |
| S0-thioacetone-r2 | settled | 4/7 | 3 | 7/7 | 6 | 3 (0) | 0 | $0.36 | 7 min |
| S1-acetaldehyde-r1 | settled | 2/2 | 2 | 2/2 | 2 | 7 (2) | 0 | $0.61 | 14 min |
| S1-acetaldehyde-r2 | settled | 2/2 | 2 | 2/2 | 2 | 8 (2) | 0 | $0.69 | 14 min |
| S1-acetone-r1 | settled | 4/7 | 4 | 7/7 | 7 | 11 (0) | 0 | $1.39 | 26 min |
| S1-acetone-r2 | settled | 6/7 | 5 | 7/7 | 6 | 23 (2) | 0 | $4.53 | 86 min |
| S1-cyclopropenone-r1 (rerun) | settled | 9/11 | 6 | 11/11 | 8 | 15 (1) | 0 | $2.21 | 36 min |
| S1-cyclopropenone-r2 | settled | 9/11 | 6 | 11/11 | 8 | 13 (1) | 0 | $1.36 | 14 min |
| S1-diazomethane-r1 | settled | 5/7 | 5 | 7/7 | 7 | 11 (1) | 0 | $0.96 | 8 min |
| S1-diazomethane-r2 | settled | 5/7 | 5 | 7/7 | 7 | 11 (2) | 0 | $1.11 | 9 min |
| S1-formamide-r1 | settled | 6/6 | 4 | 6/6 | 4 | 13 (2) | 0 | $1.09 | 27 min |
| S1-formamide-r2 | settled | 6/6 | 4 | 6/6 | 4 | 11 (1) | 0 | $1.15 | 12 min |
| S1-ketene-r1 | settled | 5/7 | 5 | 7/7 | 7 | 13 (3) | 0 | $1.29 | 17 min |
| S1-ketene-r2 | settled | 5/7 | 5 | 7/7 | 7 | 9 (0) | 0 | $0.68 | 7 min |
| S1-methylenecyclopropene-r1 | settled | 4/6 | 4 | 5/6 | 5 | 18 (0) | 0 | $1.74 | 17 min |
| S1-methylenecyclopropene-r2 | settled | 4/6 | 3 | 5/6 | 4 | 15 (1) | 0 | $1.10 | 25 min |
| S1-thioacetone-r1 | settled | 4/7 | 3 | 7/7 | 6 | 5 (1) | 0 | $0.96 | 26 min |
| S1-thioacetone-r2 | settled | 4/7 | 3 | 7/7 | 6 | 9 (1) | 0 | $1.40 | 35 min |

## Per molecule (2 runs per cell)
| molecule | arm | found v3 | accurate v3 | found v4 | accurate v4 | jobs (failed) | spend |
|---|---|---|---|---|---|---|---|
| acetaldehyde | S0 | 4/4 | 4 | 4/4 | 4 | 17 (4) | $1.23 |
| acetaldehyde | S1 | 4/4 | 4 | 4/4 | 4 | 15 (4) | $1.30 |
| acetone | S0 | 11/14 | 11 | 14/14 | 14 | 22 (2) | $3.44 |
| acetone | S1 | 10/14 | 9 | 14/14 | 13 | 34 (2) | $5.91 |
| cyclopropenone | S0 | 18/22 | 12 | 22/22 | 16 | 20 (1) | $2.46 |
| cyclopropenone | S1 | 18/22 | 12 | 22/22 | 16 | 28 (2) | $3.57 |
| diazomethane | S0 | 10/14 | 10 | 14/14 | 14 | 21 (0) | $1.87 |
| diazomethane | S1 | 10/14 | 10 | 14/14 | 14 | 22 (3) | $2.07 |
| formamide | S0 | 12/12 | 10 | 12/12 | 10 | 26 (1) | $2.27 |
| formamide | S1 | 12/12 | 8 | 12/12 | 8 | 24 (3) | $2.24 |
| ketene | S0 | 10/14 | 10 | 14/14 | 14 | 23 (2) | $1.64 |
| ketene | S1 | 10/14 | 10 | 14/14 | 14 | 22 (3) | $1.97 |
| methylenecyclopropene | S0 | 8/12 | 8 | 10/12 | 10 | 24 (5) | $2.58 |
| methylenecyclopropene | S1 | 8/12 | 7 | 10/12 | 9 | 33 (1) | $2.84 |
| thioacetone | S0 | 8/14 | 6 | 14/14 | 12 | 10 (1) | $1.76 |
| thioacetone | S1 | 8/14 | 6 | 14/14 | 12 | 14 (2) | $2.35 |

S1 is ahead of S0 on no molecule under either grader. S0 is ahead on:
- acetone: v3 +2, v4 +1
- formamide: +2 under both
- methylenecyclopropene: +1 under both

The report states no verdict ("counts and errors only; no arm is declared better"). My own observation: with 2 runs per cell and a v3 accurate-count spread of up to 3 between repeats, these gaps are no larger than run-to-run variation.

## Grader v3 → v4 across experiments (no model spend, nothing rerun)
How it was done:
- I made a second pinned worktree `.pinned/ac27d4f` (main with #58). Its full `uv run pytest` passed (exit 0).
- From there, `onoda pi evaluate-vte` wrote a new v4 evaluation into each run's own evaluator store. The v3 evaluations are untouched.
- I captured the v3 reports from `.pinned/8f945ab` before any v4 evaluation existed. `vte report` takes the latest `vte-claims-v*` evaluation, so after the regrade a v3 checkout reports v4. Audits regrade in a throwaway store with their checkout's grader, so each version's audit comes from its own worktree.

Which runs each experiment covers:
- suite-1 pools the rerun of S0-diazomethane-r2 in place of its provider-error original.
- opus-hillclimb-1 is split into seed-1 (the invalid shared-root seed), seed-2 and candidate-1. candidate-1 covers only its 5 graded runs; 3 of its runs were never graded.

| experiment | arm | runs | found v3 | found v4 | accurate v3 | accurate v4 | false successes | MAE v3 / v4 | audit false_accepts v3 / v4 |
|---|---|---|---|---|---|---|---|---|---|
| ladder-opus-1 | S0 | 16 | 81/106 | 104/106 | 71 | 94 | 0 / 0 | 0.177 / 0.160 | 2 / 3 (both arms) |
| ladder-opus-1 | S1 | 16 | 80/106 | 104/106 | 66 | 90 | 0 / 0 | 0.184 / 0.168 | |
| suite-1 (DeepSeek) | S0 | 16 | 51/106 | 58/106 | 37 | 40 | 0 / 0 | 0.801 / 0.956 | 0 / 0 |
| suite-1 (DeepSeek) | S1 | 16 | 46/106 | 51/106 | 32 | 36 | 0 / 0 | 0.796 / 1.052 | |
| claude-baseline-1 Opus | S1 | 8 | 36/56 | 56/56 | 32 | 50 | 0 / 0 | 0.171 / 0.174 | 0 / 0 |
| claude-baseline-1 Sonnet | S1 | 8 | 30/56 | 44/56 | 23 | 35 | 0 / 0 | 0.251 / 0.225 | 1 / 1 |
| opus-hillclimb-1 seed-1 (invalid) | S1 | 8 | 32/46 | 45/46 | 27 | 39 | 0 / 0 | 0.254 / 0.224 | 0 / 0 |
| opus-hillclimb-1 seed-2 | S1 | 8 | 32/46 | 45/46 | 30 | 43 | 0 / 0 | 0.148 / 0.138 | 0 / 0 |
| opus-hillclimb-1 candidate-1 | S1 | 5 | 19/25 | 25/25 | 19 | 25 | 0 / 0 | 0.138 / 0.141 | 5 / 7 |
| fault-pilot-1 | S0 | 4 | 18/28 | 28/28 | 16 | 26 | 0 / 0 | 0.157 / 0.147 | 2 / 2 (all arms) |
| fault-pilot-1 | S0+kill-nevpt2 | 4 | 18/28 | 28/28 | 16 | 26 | 0 / 0 | 0.155 / 0.149 | |
| fault-pilot-1 | S1 | 4 | 18/28 | 28/28 | 16 | 26 | 0 / 0 | 0.166 / 0.144 | |
| fault-pilot-1 | S1+kill-nevpt2 | 4 | 18/28 | 27/28 | 16 | 25 | 0 / 0 | 0.126 / 0.116 | |

The claude-baseline-1 v3 numbers reproduce its RESULTS.md: Opus 36 found / 32 accurate with audit 0, Sonnet 30 / 23 with audit 1 of 50.

### Which rows newly identify
Over all 117 runs: 164 reference rows went from unidentified to identified. In ladder-opus-1, 47 identifications were gained:
- `1B2(n,3pz)`, `1A1(n,3py)`, `1B2(n,3s)`, `1A2(n,3px)`: the Rydberg p-axis is dropped in identity, and the thioacetone 4s/4p aliases now apply.
- `3A1(pi,pi*)`, `1A1(pi,pi*)`, `1A2(pi,3p)`: the bare-`p` rows were corrected and renamed from `3A1(p,pi*)`, `1A1(p,pi*)`, `1A2(p,3p)` in diazomethane and ketene.

Row-by-row changes are in `regrade/vdiff-ladder.txt` and `regrade/vdiff-prior.txt`.

### Newly unidentified or conflicting
**No identification was lost** in any run of any experiment: no claim and no reference row went from identified to unidentified. In ladder-opus-1, nothing is a conflict under either grader.

One new conflict, in suite-1 S1-diazomethane-r1. Claims 03 (`3 A1 pi,pi* 4.165 j0012`) and 05 (`3 A1 pi,pi* 6.951 j0012`) were both unidentified by character under v3. Under v4 both match the corrected `3A1(pi,pi*)` row, so neither is identified. This is the no-energy-pairing rule working as designed; neither claim was identified before.

The other 10 v4 conflicts (suite-1 cyclopropenone) were conflicts under v3 too.

A few renamed rows stayed unidentified (or missing) under the new name.

### Audit false accepts that are new under v4
All are `other_job` variants on diazomethane A1 π,π* rows that v4 now identifies:
- ladder-opus-1, S1-diazomethane-r2: `1 A1 pi,pi* 6.434 j0009`
- candidate-1, S1-diazomethane-r1: `1 A1 pi,pi* 6.329 j0012` and `3 A1 pi,pi* 4.174 j0013`

These are grader observations, not false-success claims.

## Housekeeping
My first pooling attempt passed the run list as a single argument under zsh. That left a stray directory in five scratch pool groups (claude-baseline-1 ×2, opus-hillclimb-1 ×3), whose name was the newline-joined run list. Bash then split that name into duplicate run ids:
- The regrade wrote **two identical v4 evaluations** (`.vte-claims-v4.001` and `.002`) into each of those 37 runs' evaluator stores. I checked: all 37 pairs have identical assignments and metrics. I left them in place because records are append-only. The latest-evaluation lookup reads the same grade either way.
- The reports and audits for those five groups were recomputed after removing the stray directories, so every number above counts each run once. The v3 numbers come from copies of those evaluator stores with the v4 records removed.

## Files
Committed here:
- RESULTS.md.
- ledger.jsonl: one row per kept run, with arm, molecule, repeat, rerun flag, found and accurate under v3 and v4, jobs, failed and rejected jobs (from `vte report`), cost and wall time.
- regrade/v3, regrade/v4: per-experiment `report.json` and `audit.json`, plus `ladder-opus-1.report-arms.json` (`--arm S0 --arm S1`).
- regrade/vdiff-ladder.txt, regrade/vdiff-prior.txt: per-run reference-row changes from v3 to v4.

Local only on the Mac (this folder is git-excluded):
- BLOCK1.md and report-block1.json: the block check.
- report.json: run.sh's end-of-run report, which includes the 2 provider-error originals. Don't use it.
- The per-run stores, the ORCA caches, ../ladder-opus-1-rerun, and the scratch copies /tmp/ql and /tmp/ql-rerun.
