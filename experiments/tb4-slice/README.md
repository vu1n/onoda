# Terminal-Bench 4 paired slice: Pi (S1) vs SoL-Pi (S3)

Exploratory (docs/ASSUMPTIONS.md A54). The question is narrow: on tasks long
enough to produce build and test logs, do SoL-Pi's reducer, compaction and
Action Fusion fire at all, and what does a paired run cost with and without
them? On the six short ORCA cases the reducer and compaction never fired.

Reference point, not a target: the SoL-Pi paper (arXiv 2609.20519, Table 3)
reports 63 CPU-only Terminal-Bench 4 tasks, Pi 18 solved for $286.45 and
SoL-Pi 15 solved for $211.12 (26.3% lower cost), on GPT-5.6 Sol. This slice is
ten operator-picked tasks, one attempt, one cheap model; it does not
reproduce that table.

## What runs where

- Harbor (the benchmark's own harness) builds each task container and runs
  its verifier. `onoda.adapters.tb.harbor_agent:OnodaPiAgent` is the agent.
- The pinned Pi (0.85.1), SoL-Pi (S3 only) and onoda's recorder run inside
  the task container; Pi's `bash` tool never runs on the host.
- The provider key stays on the host in `onoda.adapters.tb.proxy.SpendProxy`,
  which also enforces the per-run and whole-slice spend caps. It is read
  from the environment variable you name and written nowhere.

## Run

```bash
scripts/pi-setup.sh
uvx harbor download terminal-bench/terminal-bench@4.0.0 -o scratch/tb4/dataset
uv run python -m onoda.adapters.tb.slice build-runtime --out scratch/tb4/runtime
export OPENROUTER_API_KEY=...
uv run python -m onoda.adapters.tb.slice run \
  --tasks-root scratch/tb4/dataset/terminal-bench --tasks experiments/tb4-slice/tasks.txt \
  --out scratch/tb4/slice-1 --runtime scratch/tb4/runtime --arms S1,S3 \
  --provider openrouter --model deepseek/deepseek-v4-flash \
  --reducer-model deepseek/deepseek-v4-flash \
  --credential-env OPENROUTER_API_KEY --upstream https://openrouter.ai \
  --max-requests 120 --max-cost-usd 0.20 --timeout-sec 1200 --slice-max-cost-usd 5.00
uv run python -m onoda.adapters.tb.slice report --out scratch/tb4/slice-1
```

Each round is one task with both arms running concurrently. A round starts
only if the slice cap still covers both arms at the proxy's per-run cap
(1.25 x `--max-cost-usd`), so with the settings above at most ten rounds fit
in $5.00. Re-running into the same `--out` skips finished rounds and counts
what earlier invocations spent.

`--timeout-sec` replaces the benchmark's 8-hour agent budget and
`--max-requests` caps provider requests (reducer and summary calls included),
so a run that stops on either is a truncated attempt, not the benchmark's
verdict on the harness.
