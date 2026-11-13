# TorchLens / local GEPA causal-search feasibility pilot

Pilot, November 13, 2026. No API spend. This was executed as a standalone feasibility experiment. **It is not integrated into Onoda's journal/replay system.**

## Reproduce

Python 3.12. CPU only, two Torch threads. Weight downloads are about 1.5 GB, excluded from this bundle. Install Torch from its official CPU index, then pinned remaining dependencies:

```sh
python -m venv .venv
.venv/bin/pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/python download_models.py
.venv/bin/python pilot.py
.venv/bin/python torchlens_check.py
.venv/bin/python gepa_pilot.py
.venv/bin/python test_pilot.py
```

GEPA run directories are checkpointed. Use an empty working directory for an independent rerun. `gepa_pilot.py` imports `pilot.py` and reruns that baseline before optimizing; those extra baseline calls are reported separately, not hidden in GEPA's search budget. TorchLens is tested separately from the efficient raw-PyTorch head-output patch evaluator: the latter is **not** a TorchLens performance measurement.

## Primary sources

- [TorchLens](https://github.com/johnmarktaylor91/torchlens), official PyPI 3.0.3: tracing and causal intervention APIs inspected locally and used on actual GPT-2.
- [GEPA](https://github.com/gepa-ai/gepa), official PyPI 0.1.4: actual optimizer with locally generated proposals based on measured feedback; no hand-written proposal pretending to be GEPA.
- [Original IOI paper](https://arxiv.org/abs/2211.00593), November 1, 2022: known GPT-2 circuit, faithfulness/completeness/minimality framing. The fixed trio uses known name-mover heads, not a new finding.
- [GPT-2 weights](https://huggingface.co/openai-community/gpt2/tree/607a30d783dfa663caf39e06633721c8d4cfcd7e), pinned revision, safetensors.
- [Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/tree/7ae557604adf67be50417f59c2c2f167def9a775), pinned local reflector, safetensors.

## Interpretation limits

There are only 24 clean prompts: 8 training, 8 validation, 8 test. Two template families per split; same four name trios across splits, all single tokens. This holds out template families, **not names**. Primary corruption changes the repeated subject (answer reversal); alternate corruption introduces a third name and is not equivalent to a uniquely determined inverse IOI target. Use it as distribution-shift stress, not universal causal truth.

Patches replace three heads at all token positions. Reported sufficiency is restored fraction of the aggregate clean-minus-corrupt logit gap. Necessity is the fraction destroyed when those clean heads receive corrupt activations. These do not establish edge-level circuit completeness or minimality, and a three-head patch can change the output without accounting for the full IOI circuit.

GEPA is seeded with the known trio, while random searches all 144 heads. Thus a GEPA result equal to the known trio is no discovery. Random selects on training only, GEPA can use validation. Count true scored examples and engine budget overshoot, not nominal candidate count. This small study cannot establish superiority.

The frozen `DESIGN.md` was written before baseline execution. A separately timestamped `GEPA-ADDENDUM.md` records local-reflector configuration after baseline completion. Test results were not used to change candidate space or metrics, but this is a sequential feasibility run, not a prospective independent benchmark.

## Scope, provenance and checks

This directory is self-contained and opt-in. It does not add dependencies to the Onoda package, move an optimizer seed, or claim a promotion-gate pass. The checked-in JSON contains numeric evidence and summarized proposal validity, not raw reflection output or execution logs. GPT-2's model card specifies MIT; Qwen2.5-0.5B-Instruct specifies Apache-2.0. Weights are downloaded from the pinned upstream revisions and are not redistributed here. This directory does not introduce a new repository-wide license.

The staged baseline was rerun and reproduced every saved score, control, candidate selection and evaluation count exactly. TorchLens fidelity and intervention checks were rerun successfully. Six offline evidence tests pass; Ruff lint and format checks pass with the repository's E/F/I/B/UP, Python 3.11, 100-column settings. Feature-map lookup reports no existing mapped feature touched. The full Onoda suite and repository-wide pointer check were not run in this standalone staging workspace.
