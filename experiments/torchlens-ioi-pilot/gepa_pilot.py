# Separate, explicitly local reflective GEPA arm; no network/API calls.
import json
import os
import time

os.environ["HF_HUB_OFFLINE"] = "1"
import gepa
import pilot as p
import torch
from gepa.core.adapter import EvaluationBatch
from transformers import AutoModelForCausalLM, AutoTokenizer

T = time.perf_counter()
tok = AutoTokenizer.from_pretrained("reflector", local_files_only=True)
lm = AutoModelForCausalLM.from_pretrained("reflector", local_files_only=True).eval()
proposals = []
evals = []


def parse(text):
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    a = json.loads(text)
    # Lossless normalization of decimal digit strings is allowed, logged, not a proposal rewrite.
    a = [int(v) if isinstance(v, str) and v.isdigit() else v for v in a]
    assert len(a) == 3 and len(set(a)) == 3 and all(type(v) is int and 0 <= v < 144 for v in a)
    return a


class Adapter:
    propose_new_texts = None

    def evaluate(self, batch, candidate, capture_traces=False):
        ids = parse(candidate["heads"])
        heads = [divmod(v, 12) for v in ids]
        outputs = []
        scores = []
        for split in sorted(set(s for s, i in batch)):
            inds = [i for s, i in batch if s == split]
            a = p.D[split]["clean"]
            b = p.D[split]["swap"]
            x = {k: v[inds] for k, v in b[0].items()}
            src = {k: v[inds] for k, v in a[4].items()}
            g, _ = p.run(x, b[1][inds], [b[2][i] for i in inds], patch=heads, source=src)
            for i, gg in zip(inds, g, strict=True):
                den = float((a[3] - b[3]).mean())
                score = float((gg - b[3][i]) / den)
                out = {"split": split, "index": i, "score": score, "heads": ids}
                outputs.append(out)
                scores.append(score)
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


def propose(candidate, reflective_dataset, components_to_update):
    old = parse(candidate["heads"])
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
    start = time.perf_counter()
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
    try:
        ids = parse(text)
        valid = True
    except Exception:
        ids = old
        valid = False
    proposals.append({"normalized": ids, "valid": valid, "seconds": time.perf_counter() - start})
    return {"heads": json.dumps(ids)}


# Smoke normalization is necessary but does not establish useful reflection.
smoke = json.load(open("reflector-smoke-summary.json"))
assert all([int(v) for v in x["normalized_candidate"]] != x["old"] for x in smoke)
results = []
for seed in [int(v) for v in os.environ.get("GEPA_SEEDS", "11").split(",")]:
    torch.manual_seed(seed)
    before = len(evals)
    propbefore = len(proposals)
    result = gepa.optimize(
        seed_candidate={"heads": "[114,117,120]"},
        trainset=[("train", i) for i in range(8)],
        valset=[("val", i) for i in range(8)],
        adapter=Adapter(),
        custom_candidate_proposer=propose,
        max_metric_calls=128,
        reflection_minibatch_size=2,
        skip_perfect_score=False,
        seed=seed,
        run_dir=f"gepa-attempt2-seed-{seed}",
        display_progress_bar=False,
    )
    ids = parse(result.best_candidate["heads"])
    n = sum(len(v["batch"]) for v in evals[before:])
    heads = [divmod(v, 12) for v in ids]
    for kind in ["swap", "abc"]:
        for necessity in [False, True]:
            results.append(
                {
                    "seed": seed,
                    "heads": heads,
                    "metric_examples": n,
                    "proposal_count": len(proposals) - propbefore,
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
        },
        open("gepa-results.json", "w"),
        indent=2,
    )
    print("GEPA DONE", seed, ids, n, flush=True)
