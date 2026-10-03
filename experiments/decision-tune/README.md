# Tuning the shadow question wording (A70)

On graded VTE runs (the real use):

    onoda vte shadow-questions --out q.json
    onoda vte shadow-dataset <run ids> --store <agent store> --evaluator-store <evaluator store> --out ds.jsonl
    onoda decide tune --dataset ds.jsonl --questions q.json --provider jev \
      --reflection-model deepseek/deepseek-v4-flash --max-reflections 8 \
      --max-decision-calls 600 --max-cost-usd 1 --out tune --enable-live-decisions
    onoda vte shadow <run> ... --instructions tune/best-instructions.json --enable-live-decisions

Rerunning `decide tune` with the same `--out` replays cached answers, so an
interrupted search resumes for free.

Plumbing check without a run: `uv run python experiments/decision-tune/fixture_dataset.py ds.jsonl`
builds 12 examples from the ORCA 6.1.1 fixtures, hand-labelled from
`tests/fixtures/orca611-nevpt2/README.md` (only `ketene_f_target` is "ready";
the two no-symmetry NEVPT2 jobs are left out). These labels are not the
grader's; a result on them shows the plumbing, not a tuned prompt.
