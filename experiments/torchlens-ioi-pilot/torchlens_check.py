import json
import os
import time

os.environ["HF_HUB_OFFLINE"] = "1"
import torch
import torchlens as tl
from transformers import AutoTokenizer, GPT2LMHeadModel

torch.set_num_threads(2)
tok = AutoTokenizer.from_pretrained("model", local_files_only=True)
m = GPT2LMHeadModel.from_pretrained(
    "model", local_files_only=True, attn_implementation="eager"
).eval()
a = tok("When John and Mary went to the store, Mary gave a bottle to", return_tensors="pt")[
    "input_ids"
]
b = tok("When John and Mary went to the store, John gave a bottle to", return_tensors="pt")[
    "input_ids"
]
with torch.no_grad():
    raw = m(a, use_cache=False).logits
    start = time.perf_counter()
    clean = tl.trace(m, a, input_kwargs={"use_cache": False})
    secs = time.perf_counter() - start
    print("trace seconds", secs, flush=True)
    logged = clean["lm_head"].out
    print("max no op error", (logged - raw).abs().max().item(), flush=True)
    corrupt = tl.trace(m, b, input_kwargs={"use_cache": False})
    print("corrupt traced", flush=True)
    patched = corrupt.fork().do(tl.module("transformer.ln_f"), tl.patch_from(clean))
    err = (patched["lm_head"].out - raw).abs().max().item()
    print("full residual patch error", err, flush=True)
    # Separately verify TorchLens causal head edit matches raw pre-c_proj patch.
    selector = tl.module("transformer.h.9.attn") & tl.head(6, "v")
    try:
        headpatched = corrupt.fork().do(selector, tl.patch_from(clean))
        delta = (headpatched["lm_head"].out - corrupt["lm_head"].out).abs().max().item()
        headdata = {
            "status": "completed",
            "max_logit_change": delta,
            "note": "v patch, not the output patch used in baseline search",
        }
    except Exception as e:
        headdata = {"status": "failed", "error": repr(e)}
    result = {
        "torchlens_version": tl.__version__,
        "trace_seconds": secs,
        "no_op_max_abs_error": float((logged - raw).abs().max()),
        "residual_patch_max_abs_error": err,
        "head_patch": headdata,
    }
    json.dump(result, open("torchlens-check.json", "w"), indent=2)
    assert result["no_op_max_abs_error"] < 1e-5 and err < 1e-5
