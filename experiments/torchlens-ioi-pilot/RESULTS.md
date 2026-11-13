# Results: feasible instrumentation, not a GEPA win

## What really ran
- GPT-2 small safetensors at pinned revision; CPU only, two threads.
- 24 prompts, six template families split 2/2/2 among train/validation/test; four name trios reused between splits.
- Fixed known name-mover trio and 16 random three-head candidates per seed, three seeds. Selection uses only training primary-corruption recovery.
- Real TorchLens tracing and causal edits, separately from raw PyTorch head-output patch evaluation.
- Actual GEPA 0.1.4 with a local Qwen2.5-0.5B-Instruct reflective proposer. One completed seed; search failed operationally because it produced no distinct valid candidate.

## Instrumentation controls
TorchLens logged output exactly matched raw GPT-2 logits (max absolute error 0). Patching the entire final residual from clean into corrupt recovered clean logits exactly (error 0). A TorchLens value-head patch changed logits (maximum change 0.2109). Trace time was 2.388 seconds for one example, not a comparative latency benchmark.

The raw intervention engine also passed identity and final-residual recovery controls on all six split × corruption combinations. These demonstrate correct plumbing, not a correct full circuit explanation.

## Locked test results
Each arm patches three heads. Values below are fractions of the mean clean-minus-corrupt logit gap, not accuracy. Primary test gap was 5.8469 logit units; alternate was 2.5796.

| Arm | Primary gap restored | Alternate gap restored | Primary gap destroyed | Alternate gap destroyed |
|---|---:|---:|---:|---:|
| Known trio (9.6, 9.9, 10.0) | 23.1% | 34.1% | 28.4% | 15.4% |
| Random, seed 11 | 33.2% | 9.9% | 15.5% | 14.7% |
| Random, seed 22 | 16.2% | 19.8% | 10.2% | 15.8% |
| Random, seed 33 | 24.5% | 22.7% | 13.5% | 21.2% |

The strongest random primary-corruption candidate lost to the known trio under alternate corruption. That is the interesting warning: optimizing one patch distribution can select a different explanation from one that transfers. This is only eight test examples over two template families. Seed ranges are descriptive; no credible broad generalization or significance claim follows.

The fixed/raw baseline run took 33.09 seconds including model load, caches, controls, search and final evaluation, 117 batched forwards. Each random seed spent 128 scored training examples; test/validation reporting is separate. This establishes a cheap executable testbed, not a full IOI circuit discovery.

## GEPA outcome
Completed seed 11: 128 scored examples, 30 local reflection calls, 118.38 seconds including reflector loading and test evaluation. Of 30 generations, 29 violated the exact three-integer candidate schema; the sole valid generation repeated the seed. Zero distinct valid candidate proposals. GEPA retained the known trio, so its final scores equal that trio **by default, not because it discovered or improved it**.

The trivial smoke test generated distinct IDs after lossless numeric-string normalization, but the realistic feedback prompt failed. This is a schema-following / reflector capability blocker. It is not evidence that GEPA cannot optimize circuits. Seed 22 was stopped early and seed 33 not run rather than wasting compute on the same defect. An initial adapter wiring failure produced no successful optimizer evidence and is excluded.

## What is worth doing next
1. Keep this as a mechanics feasibility result, not a claim of circuit discovery.
2. Before any serious search, require the reflector to pass a realistic feedback-to-candidate smoke test. A stronger local model or an explicitly budgeted external model is needed; no paid call was made here.
3. Compare reflection and random with the same seed prior, validation policy and exact evaluation budget. Expand held-out template and name groups. Optimize robustness across training corruption families, leaving a genuinely separate corruption family for final testing.
4. Integrate artifacts with Onoda's existing journals/evaluator separation only after the evaluator and proposal contract are solid. This run was isolated scratch, not an Onoda-native run.

This payload adds experiment scripts and evidence only; it does not change the Onoda runtime.
