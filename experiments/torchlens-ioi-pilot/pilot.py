import os

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import json
import random
import time

import numpy as np
import torch
from transformers import AutoTokenizer, GPT2LMHeadModel

T = time.perf_counter()
torch.set_num_threads(2)
torch.manual_seed(0)
tok = AutoTokenizer.from_pretrained("model", local_files_only=True)
tok.pad_token = tok.eos_token
model = GPT2LMHeadModel.from_pretrained(
    "model", local_files_only=True, attn_implementation="eager"
).eval()
templates = {
    "train": [
        "When {A} and {B} went to the store, {S} gave a bottle to",
        "After {A} and {B} went to the park, {S} gave a book to",
    ],
    "val": [
        "Then {A} and {B} visited the museum, and {S} handed a ticket to",
        "While {A} and {B} were at the school, {S} passed a note to",
    ],
    "test": [
        "Yesterday {A} and {B} met at the station, and {S} gave a letter to",
        "At the restaurant, {A} and {B} sat together. {S} offered a drink to",
    ],
}
names = [
    ("John", "Mary", "Tom"),
    ("Alice", "Bob", "James"),
    ("Sarah", "David", "Michael"),
    ("Robert", "Susan", "Jack"),
]
for ns in names:
    for n in ns:
        assert len(tok.encode(" " + n)) == 1, (n, tok.encode(" " + n))
counts = {"forwards": 0}
rows = []


def batch(split, kind):
    texts = []
    ans = []
    for template in templates[split]:
        for A, B, C in names:
            texts.append(
                template.format(A=A, B=B, S=B if kind == "clean" else A if kind == "swap" else C)
            )
            ans.append((tok.encode(" " + A)[0], tok.encode(" " + B)[0]))
    x = tok(texts, return_tensors="pt", padding=True)
    last = x["attention_mask"].sum(1) - 1
    return x, last, ans, texts


@torch.inference_mode()
def run(x, last, ans, cache=False, patch=None, source=None, final=False):
    hs = []
    out = {}
    for layer in range(12):

        def hook(mod, inputs, layer_index=layer):
            a = inputs[0]
            if cache:
                out[layer_index] = a.detach().clone()
            heads = [h for ll, h in (patch or []) if ll == layer_index]
            if heads:
                a = a.clone()
                for h in heads:
                    a[:, :, h * 64 : (h + 1) * 64] = source[layer_index][
                        :, :, h * 64 : (h + 1) * 64
                    ]
                return (a,) + inputs[1:]

        hs.append(model.transformer.h[layer].attn.c_proj.register_forward_pre_hook(hook))
    if cache:
        hs.append(
            model.transformer.ln_f.register_forward_hook(
                lambda m, i, o: out.update(final=o.detach().clone())
            )
        )
    if final:
        hs.append(
            model.transformer.ln_f.register_forward_hook(lambda m, i, o: source["final"].clone())
        )
    try:
        y = model(**x, use_cache=False).logits
        counts["forwards"] += 1
    finally:
        for h in hs:
            h.remove()
    z = y[torch.arange(len(last)), last]
    gap = (
        z[torch.arange(len(ans)), [a for a, b in ans]]
        - z[torch.arange(len(ans)), [b for a, b in ans]]
    ).numpy()
    return gap, out


D = {}
controls = {}
for split in templates:
    D[split] = {}
    for kind in ["clean", "swap", "abc"]:
        x, last, ans, texts = batch(split, kind)
        g, c = run(x, last, ans, True)
        D[split][kind] = (x, last, ans, g, c)
        for i, _text in enumerate(texts):
            rows.append({"split": split, "kind": kind, "index": i, "gap": float(g[i])})
    for kind in ["swap", "abc"]:
        clean = D[split]["clean"]
        corrupt = D[split][kind]
        assert clean[0]["input_ids"].shape == corrupt[0]["input_ids"].shape
        assert torch.equal(clean[0]["attention_mask"], corrupt[0]["attention_mask"])
        identity, _ = run(
            *corrupt[:3],
            patch=[(layer_index, h) for layer_index in range(12) for h in range(12)],
            source=corrupt[4],
        )
        pos, _ = run(*corrupt[:3], source=clean[4], final=True)
        controls[split + "_" + kind] = {
            "identity_max_error": float(np.max(abs(identity - corrupt[3]))),
            "final_residual_recovery_max_error": float(np.max(abs(pos - clean[3]))),
        }
        assert controls[split + "_" + kind]["identity_max_error"] < 1e-5
        assert controls[split + "_" + kind]["final_residual_recovery_max_error"] < 1e-5
print(
    "DATA", [(s, {k: float(v[3].mean()) for k, v in ds.items()}) for s, ds in D.items()], flush=True
)


def evaluate(heads, split, kind, necessity=False):
    clean = D[split]["clean"]
    corrupt = D[split][kind]
    target, source = (clean, corrupt) if necessity else (corrupt, clean)
    g, _ = run(*target[:3], patch=heads, source=source[4])
    den = float(np.mean(clean[3] - corrupt[3]))
    num = float(np.mean(clean[3] - g) if necessity else np.mean(g - corrupt[3]))
    return {
        "fraction": num / den if abs(den) > 1e-5 else None,
        "denominator": den,
        "gap": float(g.mean()),
        "per_example_gap": g.tolist(),
    }


fixed = [(9, 6), (9, 9), (10, 0)]
candidates = [{"arm": "fixed_known_ioi", "heads": fixed, "search_evals": 0}]
search = []
for seed in [11, 22, 33]:
    rng = random.Random(seed)
    best = None
    for trial in range(16):
        heads = sorted(
            rng.sample([(layer_index, h) for layer_index in range(12) for h in range(12)], 3)
        )
        res = evaluate(heads, "train", "swap")
        v = {"seed": seed, "trial": trial, "heads": heads, **res}
        search.append(v)
        if best is None or res["fraction"] > best["fraction"]:
            best = v
    candidates.append({"arm": "random", "seed": seed, "heads": best["heads"], "search_evals": 16})
    print("SEARCH", seed, best, flush=True)
results = []
for c in candidates:
    for split in templates:
        for kind in ["swap", "abc"]:
            for necessity in [False, True]:
                results.append(
                    {
                        **c,
                        "split": split,
                        "corruption": kind,
                        "measure": "necessity" if necessity else "sufficiency",
                        **evaluate(c["heads"], split, kind, necessity),
                    }
                )
report = {
    "model": "openai-community/gpt2",
    "revision": "607a30d783dfa663caf39e06633721c8d4cfcd7e",
    "torch": torch.__version__,
    "cpu_threads": 2,
    "controls": controls,
    "data": rows,
    "search": search,
    "results": results,
    "counts": counts,
    "wall_seconds": time.perf_counter() - T,
    "gepa": (
        "Separate arm; see gepa-completed-seed11.json and RESULTS.md. "
        "No GEPA inference follows from this baseline file."
    ),
}
json.dump(report, open("results.json", "w"), indent=2)
print("DONE", report["wall_seconds"], counts, flush=True)
