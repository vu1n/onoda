# Separate, explicitly local reflective GEPA arm; no network/API calls.
# ruff: noqa: E402 -- offline/preflight guards precede model-loading imports.
import json
import os
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
SEEDS = [int(v) for v in os.environ.get("GEPA_SEEDS", "11").split(",")]
if Path("gepa-guarded-results.json").exists() or any(
    Path(f"gepa-guarded-seed-{seed}").exists() for seed in SEEDS
):
    raise FileExistsError("Use a fresh working directory; guarded runs do not resume")
T = time.perf_counter()
import gepa
import pilot as p
import torch
from gepa.core.adapter import EvaluationBatch
from search_control import BatchCache, ProposalGuard, parse
from transformers import AutoModelForCausalLM, AutoTokenizer

pilot_setup_seconds = time.perf_counter() - T
tok = AutoTokenizer.from_pretrained("reflector", local_files_only=True)
lm = AutoModelForCausalLM.from_pretrained("reflector", local_files_only=True).eval()
reflector_setup_seconds = time.perf_counter() - T - pilot_setup_seconds
proposals = []
evals = []
cache_runs = []


class Adapter:
    propose_new_texts = None

    def __init__(self):
        self.cache = BatchCache(self._evaluate)

    def evaluate(self, batch, candidate, capture_traces=False):
        return self.cache(batch, candidate, capture_traces)

    def _evaluate(self, batch, candidate, capture_traces=False):
        ids = parse(candidate["heads"])
        heads = [divmod(v, 12) for v in ids]
        by_position = {}
        for split in sorted(set(s for s, i in batch)):
            inds = [i for s, i in batch if s == split]
            a = p.D[split]["clean"]
            b = p.D[split]["swap"]
            x = {k: v[inds] for k, v in b[0].items()}
            src = {k: v[inds] for k, v in a[4].items()}
            g, _ = p.run(x, b[1][inds], [b[2][i] for i in inds], patch=heads, source=src)
            positions = [pos for pos, (s, _) in enumerate(batch) if s == split]
            for pos, i, gg in zip(positions, inds, g, strict=True):
                den = float((a[3] - b[3]).mean())
                score = float((gg - b[3][i]) / den)
                out = {"split": split, "index": i, "score": score, "heads": ids}
                by_position[pos] = out
        outputs = [by_position[pos] for pos in range(len(batch))]
        scores = [out["score"] for out in outputs]
        evals.append({"candidate": ids, "batch": batch, "scores": scores})
        return EvaluationBatch(outputs, scores, outputs if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        return {
            "heads": [
                {
                    "candidate": candidate["heads"],
                    "mean_gap_recovery": sum(eval_batch.scores) / len(eval_batch.scores),
                    "feedback": (
                        "Larger recovery is better; candidate patches three heads from clean "
                        "into corrupt runs. Try a different 3-head set."
                    ),
                }
            ]
        }


def generate(candidate, reflective_dataset, components_to_update):
    prompt = (
        "You propose attention circuit experiments. Each head ID is an integer 0 to 143 "
        "(layer * 12 + head). Candidate has exactly three distinct IDs. Goal maximize recovered "
        "clean-corrupt logit gap. Use measured feedback to suggest a new set. "
        "Return only a JSON list of three IDs.\n"
    ) + json.dumps(reflective_dataset)
    inp = tok.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=False,
    )
    with torch.no_grad():
        out = lm.generate(
            inp,
            max_new_tokens=80,
            do_sample=True,
            temperature=0.8,
            top_p=0.95,
            pad_token_id=tok.eos_token_id,
        )
    text = tok.decode(out[0, inp.shape[1] :], skip_special_tokens=True)
    return text


# Smoke normalization is necessary but does not establish useful reflection.
smoke = json.load(open("reflector-smoke-summary.json"))
assert all([int(v) for v in x["normalized_candidate"]] != x["old"] for x in smoke)
results = []
for seed in SEEDS:
    torch.manual_seed(seed)
    before = len(evals)
    adapter = Adapter()
    guard = ProposalGuard(generate, int(os.environ.get("GEPA_MAX_PROPOSALS", "30")))
    result = gepa.optimize(
        seed_candidate={"heads": "[114,117,120]"},
        trainset=[("train", i) for i in range(8)],
        valset=[("val", i) for i in range(8)],
        adapter=adapter,
        custom_candidate_proposer=guard,
        stop_callbacks=guard.stopped,
        max_metric_calls=128,
        reflection_minibatch_size=2,
        skip_perfect_score=False,
        seed=seed,
        run_dir=f"gepa-guarded-seed-{seed}",
        display_progress_bar=False,
    )
    proposals.extend(guard.records)
    ids = parse(result.best_candidate["heads"])
    n = sum(v["examples"] for v in adapter.cache.records)
    physical_n = sum(len(v["batch"]) for v in evals[before:])
    cache_runs.append({"seed": seed, "requests": adapter.cache.records})
    heads = [divmod(v, 12) for v in ids]
    for kind in ["swap", "abc"]:
        for necessity in [False, True]:
            results.append(
                {
                    "seed": seed,
                    "heads": heads,
                    "metric_examples": n,
                    "physical_metric_examples": physical_n,
                    "proposal_count": len(guard.records),
                    "corruption": kind,
                    "measure": "necessity" if necessity else "sufficiency",
                    **p.evaluate(heads, "test", kind, necessity),
                }
            )
    json.dump(
        {
            "results": results,
            "proposals": proposals,
            "evals": evals,
            "wall_seconds": time.perf_counter() - T,
            "cache": cache_runs,
            "pilot_setup_seconds": pilot_setup_seconds,
            "reflector_setup_seconds": reflector_setup_seconds,
            "budget_note": "GEPA logical examples unchanged; evals records physical misses",
        },
        open("gepa-guarded-results.json", "w"),
        indent=2,
    )
    print("GEPA DONE", seed, ids, n, flush=True)
